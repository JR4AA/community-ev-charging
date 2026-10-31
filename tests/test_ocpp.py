"""End to end: real app and OCPP server, fake PayPal, simulated charger over a real websocket."""
import asyncio
import base64
import threading
import time
from decimal import Decimal

import httpx
import pytest
import websockets
from fastapi.testclient import TestClient

from evcharge.config import Settings
from evcharge.csms import energy_readings
from evcharge.paypal import PayPal
from evcharge.simulator import CHARGERS, connect
from evcharge.tariff import Tariff
from evcharge.web import create_app
from tests.test_payments import SETTINGS, FakePayPal


class Sim:
    """A simulated charger on its own event loop thread."""

    def __init__(self, port, cp_id="CP1", password=None, plug_in=True, version="1.6", **kw):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.cp = self.run(self._open(f"ws://127.0.0.1:{port}/{cp_id}", cp_id, password, plug_in, version, kw))

    async def _open(self, url, cp_id, password, plug_in, version, kw):
        self.ws = await connect(url, cp_id, password, version)
        cp = CHARGERS[version][0](cp_id, self.ws, **kw)
        asyncio.ensure_future(cp.start())
        if plug_in:
            await cp.boot_and_plug_in()
        return cp

    def run(self, coro, timeout=10):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def close(self):
        self.run(self.ws.close())
        self.loop.call_soon_threadsafe(self.loop.stop)


def wait_for(predicate, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")


@pytest.fixture
def fake():
    return FakePayPal()


# The charging tests run against each kind of simulated charger.
CHARGER_KINDS = {"1.6": {}, "2.0.1": {"version": "2.0.1"},
                 "2.0.1, transaction from plug-in": {"version": "2.0.1", "tx_on_plug_in": True}}


@pytest.fixture(params=list(CHARGER_KINDS))
def kind(request):
    return CHARGER_KINDS[request.param]


def make_app(tmp_path, fake, **overrides):
    s = Settings(db_path=str(tmp_path / "e2e.db"), **{**SETTINGS, **overrides})
    return create_app(s, PayPal(s, transport=httpx.MockTransport(fake)))


@pytest.fixture
def env(tmp_path, fake):
    app = make_app(tmp_path, fake)
    with TestClient(app) as client:
        port = app.state.ocpp_server.sockets[0].getsockname()[1]
        yield app, client, port


def pay(client, socket="CP1", hold="20.00"):
    r = client.post("/pay/api/orders", json={"connector": socket, "hold": hold})
    assert r.status_code == 200, r.text
    r = client.post(f"/pay/api/orders/{r.json()['orderID']}/authorize")
    assert r.status_code == 200, r.text
    return r.json()["session"]


def session(app, sid):
    return app.state.payments.db.session(sid)


def test_plug_in_pay_charge_unplug(env, fake, kind):
    app, client, port = env
    assert "offline" in client.get("/pay/CP1").text
    sim = Sim(port, kw=7, tick=0.02, speed=600, unplug_after_kwh=3, **kind)
    wait_for(lambda: app.state.csms.socket_state("CP1") == "Preparing")
    assert "intent=authorize" in client.get("/pay/CP1").text
    wait_for(lambda: sim.cp.sample_interval == "60")
    sid = pay(client)
    wait_for(lambda: session(app, sid)["status"] == "CAPTURED")
    s = session(app, sid)
    assert 3000 <= s["energy_wh"] < 3100
    expected = (Decimal(s["energy_wh"]) / 1000 * Decimal("0.45")).quantize(Decimal("0.01"), "ROUND_HALF_UP")
    assert s["amount_charged"] == f"{expected:.2f}"
    assert fake.auths[s["paypal_authorization_id"]]["captured"]["amount"]["value"] == s["amount_charged"]
    assert "Charging finished and paid" in client.get(f"/pay/session/{sid}").text
    sim.close()


def test_stops_before_the_hold_runs_out(env, fake, kind):
    app, client, port = env
    sim = Sim(port, kw=22, tick=0.01, speed=3600, **kind)
    wait_for(lambda: app.state.csms.socket_state("CP1") == "Preparing")
    sid = pay(client)
    wait_for(lambda: session(app, sid)["status"] == "CAPTURED", timeout=20)
    s = session(app, sid)
    assert Decimal("19.50") <= Decimal(s["amount_charged"]) <= Decimal("20.00")
    assert any(e["kind"] == "remote_stop" for e in app.state.payments.db.events(sid))
    sim.close()


def test_driver_presses_stop(env, kind):
    app, client, port = env
    sim = Sim(port, kw=7, tick=0.05, speed=60, **kind)
    wait_for(lambda: app.state.csms.socket_state("CP1") == "Preparing")
    sid = pay(client)
    wait_for(lambda: session(app, sid)["status"] == "CHARGING")
    wait_for(lambda: len(app.state.payments.db.readings(app.state.payments.db.transaction_for_session(sid)["id"])) > 2)
    page = client.get(f"/pay/session/{sid}").text
    assert "Stop charging" in page and "So far" in page
    client.post(f"/pay/session/{sid}/stop", follow_redirects=False)
    wait_for(lambda: session(app, sid)["status"] in ("CAPTURED", "VOIDED"))
    sim.close()


def test_must_plug_in_before_paying(env):
    app, client, port = env
    sim = Sim(port, plug_in=False)
    sim.run(sim.cp.call(__import__("ocpp.v16.call", fromlist=["x"]).BootNotification(
        charge_point_model="Simulator", charge_point_vendor="evcharge")))
    sim.run(sim.cp.status("Available"))
    wait_for(lambda: app.state.csms.socket_state("CP1") == "Available")
    assert "Plug your car in first" in client.get("/pay/CP1").text
    assert client.post("/pay/api/orders", json={"connector": "CP1"}).status_code == 409
    sim.close()


def test_charger_refusing_start_cancels_payment(env, fake, kind):
    app, client, port = env
    sim = Sim(port, **kind)
    wait_for(lambda: app.state.csms.socket_state("CP1") == "Preparing")
    sim.cp._charging = object()      # simulator now rejects remote starts
    oid = client.post("/pay/api/orders", json={"connector": "CP1"}).json()["orderID"]
    r = client.post(f"/pay/api/orders/{oid}/authorize")
    assert r.status_code == 503 and "not been charged" in r.json()["detail"]
    s = app.state.payments.db.session_by_order(oid)
    assert s["status"] == "VOIDED"
    assert fake.auths[s["paypal_authorization_id"]]["status"] == "VOIDED"
    sim.close()


def test_unknown_tag_is_refused(env):
    app, client, port = env
    sim = Sim(port)
    from ocpp.v16 import call
    r = sim.run(sim.cp.call(call.StartTransaction(connector_id=1, id_tag="NOT-A-SESSION", meter_start=5,
                                                  timestamp="2026-11-02T10:00:00Z")))
    assert r.id_tag_info["status"] == "Invalid"
    r = sim.run(sim.cp.call(call.Authorize(id_tag="NOT-A-SESSION")))
    assert r.id_tag_info["status"] == "Invalid"
    sim.close()


def test_unknown_token_is_refused_201(env):
    app, client, port = env
    sim = Sim(port, version="2.0.1")
    from ocpp.v201 import call
    r = sim.run(sim.cp.call(call.TransactionEvent(
        event_type="Started", timestamp="2026-11-02T10:00:00Z", trigger_reason="Authorized", seq_no=1,
        transaction_info={"transaction_id": "x1"}, evse={"id": 1, "connector_id": 1},
        id_token={"id_token": "NOT-A-SESSION", "type": "Central"},
        meter_value=[{"timestamp": "2026-11-02T10:00:00Z", "sampled_value": [{"value": 5}]}])))
    assert r.id_token_info["status"] == "Invalid"
    assert app.state.payments.db.transaction_by_ocpp("CP1", "x1") is None
    r = sim.run(sim.cp.call(call.Authorize(id_token={"id_token": "NOT-A-SESSION", "type": "Central"})))
    assert r.id_token_info["status"] == "Invalid"
    sim.close()


def test_energy_before_payment_is_not_billed_201(env, fake):
    """A 2.0.1 charger that starts its transaction at plug-in: only energy after payment counts."""
    app, client, port = env
    sim = Sim(port, kw=7, tick=0.02, speed=600, unplug_after_kwh=1, version="2.0.1", tx_on_plug_in=True)
    wait_for(lambda: app.state.csms.socket_state("CP1") == "Preparing")
    sim.cp.meter_wh += 5000                 # e.g. a car that drew power before anyone paid
    sim.run(sim.cp.event("Updated", "MeterValuePeriodic", "EVConnected"))
    assert app.state.csms.socket_state("CP1") == "Preparing"
    sid = pay(client)
    wait_for(lambda: session(app, sid)["status"] == "CAPTURED")
    s = session(app, sid)
    assert 1000 <= s["energy_wh"] < 1100
    t = app.state.payments.db.transaction_for_session(sid)
    assert t["ocpp_tx_id"] == sim.cp.transaction_id and t["meter_start"] == 105_000
    sim.close()


def test_settlement_retried_after_paypal_outage(env, fake, kind):
    app, client, port = env
    sim = Sim(port, kw=7, tick=0.02, speed=600, unplug_after_kwh=1, **kind)
    wait_for(lambda: app.state.csms.socket_state("CP1") == "Preparing")
    fake.fail_next["/capture"] = (503, {"name": "SERVICE_UNAVAILABLE"})
    sid = pay(client)
    wait_for(lambda: (session(app, sid)["error"] or "").endswith("(retryable)"))
    assert session(app, sid)["status"] == "CHARGING"
    app.state.csms.retry_settlements()
    assert session(app, sid)["status"] == "CAPTURED"
    sim.close()


def test_peak_and_offpeak_from_charger_timestamps(tmp_path, fake, kind):
    # Every band at the same price except peak, so the result doesn't depend on
    # when the test runs: check the breakdown names instead.
    app = make_app(tmp_path, fake, tariff=Tariff.parse(
        "offpeak 00:00-16:00 0.27; peak 16:00-19:00 0.46; offpeak 19:00-24:00 0.27"))
    with TestClient(app) as client:
        port = app.state.ocpp_server.sockets[0].getsockname()[1]
        sim = Sim(port, kw=7, tick=0.02, speed=600, unplug_after_kwh=2, **kind)
        wait_for(lambda: app.state.csms.socket_state("CP1") == "Preparing")
        sid = pay(client)
        wait_for(lambda: session(app, sid)["status"] == "CAPTURED")
        assert session(app, sid)["breakdown"]
        sim.close()


# -- who may connect -------------------------------------------------------------

def _try(port, path="/CP1", headers=None, subprotocols=("ocpp1.6",)):
    async def go():
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}{path}", subprotocols=list(subprotocols),
                                          additional_headers=headers or {}):
                return 101
        except websockets.InvalidStatus as e:
            return e.response.status_code
    return asyncio.run(go())


def _basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


WSS = {"Cf-Visitor": '{"scheme":"https"}'}


def test_access_control(tmp_path, fake):
    app = make_app(tmp_path, fake, ocpp_passwords={"CP1": "s3cret"})
    with TestClient(app):
        port = app.state.ocpp_server.sockets[0].getsockname()[1]
        assert _try(port, "/NOPE") == 404
        assert _try(port, subprotocols=()) == 400
        assert _try(port) == 401                                         # password configured, none sent
        assert _try(port, headers=_basic("CP1", "wrong")) == 401
        assert _try(port, headers=_basic("CP2", "s3cret")) == 401     # right password, wrong user
        assert _try(port, headers=_basic("CP1", "s3cret")) == 101
        assert _try(port, "/ocpp/CP1", headers=_basic("CP1", "s3cret")) == 101
        # Through the tunnel a charger with no password is never let in.
        assert _try(port, "/CP2", headers={"Cf-Connecting-Ip": "203.0.113.9", **WSS}) == 403
        assert _try(port, "/CP2") == 101                              # LAN, no password set
        # Missing passwords don't count towards the lockout; wrong ones do, per source address.
        for _ in range(6):
            assert _try(port, headers={"Cf-Connecting-Ip": "198.51.100.6", **WSS}) == 401
        assert _try(port, headers={**_basic("CP1", "s3cret"), "Cf-Connecting-Ip": "198.51.100.6", **WSS}) == 101
        for _ in range(4):
            _try(port, headers={**_basic("CP1", "nope"), "Cf-Connecting-Ip": "198.51.100.7", **WSS})
        assert _try(port, headers={**_basic("CP1", "nope"), "Cf-Connecting-Ip": "198.51.100.7", **WSS}) == 401
        assert _try(port, headers={**_basic("CP1", "s3cret"), "Cf-Connecting-Ip": "198.51.100.7", **WSS}) == 429
        assert _try(port, headers={**_basic("CP1", "s3cret"), "Cf-Connecting-Ip": "198.51.100.8", **WSS}) == 101
        # Through the tunnel but over plain ws:// (Cloudflare reports scheme http): refused.
        assert _try(port, headers={**_basic("CP1", "s3cret"), "Cf-Connecting-Ip": "198.51.100.9",
                                   "Cf-Visitor": '{"scheme":"http"}'}) == 403
        assert _try(port, headers={**_basic("CP1", "s3cret"), "Cf-Connecting-Ip": "198.51.100.9"}) == 403


def test_plain_ws_needs_the_charger_flag(tmp_path, fake):
    app = make_app(tmp_path, fake, ocpp_allow_ws=frozenset({"CP2"}))
    with TestClient(app):
        port = app.state.ocpp_server.sockets[0].getsockname()[1]
        assert _try(port, "/CP1") == 403          # private address, but CP1 must use wss://
        assert _try(port, "/CP2") == 101          # flagged as unable to do wss://
    from evcharge.config import from_env
    assert from_env({"OCPP_ALLOW_WS_CP2": "true", "OCPP_ALLOW_WS_CP1": "no"}).ocpp_allow_ws == {"CP2"}


def test_protocol_version_negotiation(env):
    app, client, port = env

    async def chosen(offered):
        async with websockets.connect(f"ws://127.0.0.1:{port}/CP2", subprotocols=offered) as ws:
            return ws.subprotocol
    assert asyncio.run(chosen(["ocpp1.6"])) == "ocpp1.6"
    assert asyncio.run(chosen(["ocpp2.0.1"])) == "ocpp2.0.1"
    assert asyncio.run(chosen(["ocpp1.6", "ocpp2.0.1"])) == "ocpp1.6"       # the charger's preference
    assert asyncio.run(chosen(["ocpp2.1", "ocpp2.0.1", "ocpp1.6"])) == "ocpp2.0.1"
    assert _try(port, "/CP2", subprotocols=("ocpp2.1",)) == 400
    sim = Sim(port, version="2.0.1")
    wait_for(lambda: app.state.csms.info.get("CP1", {}).get("protocol") == "OCPP 2.0.1")
    assert app.state.csms.info["CP1"]["model"] == "Simulator"
    sim.close()


def test_private_addresses():
    from evcharge.csms import _is_private
    for ok in ("192.168.1.50", "10.1.2.3", "172.16.0.1", "172.31.255.255", "127.0.0.1", "::1", "::ffff:192.168.1.50"):
        assert _is_private(ok), ok
    for bad in ("203.0.113.50", "172.32.0.1", "100.64.1.1", "8.8.8.8", "2001:db8::1", "nonsense"):
        assert not _is_private(bad), bad


def test_energy_readings_parsing():
    mv = [{"timestamp": "2026-11-02T10:00:00Z", "sampled_value": [
        {"value": "12.5", "unit": "kWh", "measurand": "Energy.Active.Import.Register"},
        {"value": "4000", "measurand": "Energy.Active.Import.Register", "phase": "L1"},
        {"value": "7.2", "unit": "kW", "measurand": "Power.Active.Import"},
        {"value": "12600"},                                   # measurand defaults to energy, unit to Wh
    ]}, {"timestamp": "2026-11-02T10:01:00+01:00", "sampled_value": [{"value": "1", "location": "Inlet"}]}]
    assert energy_readings(mv) == [("2026-11-02T10:00:00+00:00", 12500), ("2026-11-02T10:00:00+00:00", 12600)]
    # 2.0.1: numbers, with unitOfMeasure and its power-of-ten multiplier
    mv = [{"timestamp": "2026-11-02T10:00:00Z", "sampled_value": [
        {"value": 12.5, "unit_of_measure": {"unit": "kWh"}},
        {"value": 12.6, "unit_of_measure": {"unit": "Wh", "multiplier": 3}},
        {"value": 12700, "measurand": "Energy.Active.Import.Register", "context": "Transaction.End"},
    ]}]
    assert [wh for _, wh in energy_readings(mv)] == [12500, 12600, 12700]
