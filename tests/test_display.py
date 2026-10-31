"""Charger diagnostics on the admin page, and prices on the charger's screen (OCA costmsg)."""
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from tests.test_admin import AUTH, LAN, make, post
from tests.test_ocpp import Sim, pay, wait_for
from tests.test_payments import FakePayPal


@pytest.fixture
def fake():
    return FakePayPal()


def start(tmp_path, fake, screen=True, **kw):
    app = make(tmp_path, fake, public_url="https://ev.example.org", **kw)
    client = TestClient(app, client=LAN)
    client.__enter__()
    port = app.state.ocpp_server.sockets[0].getsockname()[1]
    sim = Sim(port, kw=7, tick=0.02, speed=600, screen=screen)
    wait_for(lambda: app.state.csms.socket_state("CP1") == "Preparing")
    wait_for(lambda: "CP1" in app.state.csms.config)          # settings read after connecting
    return app, client, sim


def test_prices_on_a_charger_with_a_screen(tmp_path, fake):
    app, c, sim = start(tmp_path, fake)
    sim.cp.unplug_after_wh = 2000
    wait_for(lambda: sim.cp.config["CustomDisplayCostAndPrice"] == "true")
    assert app.state.csms.display["CP1"]
    sid = pay(c)
    db = app.state.payments.db
    wait_for(lambda: db.session(sid)["status"] == "CAPTURED")
    wait_for(lambda: any(m == "FinalCost" for m, _ in sim.cp.screen))
    kinds = [m for m, _ in sim.cp.screen]
    assert kinds[0] == "RunningCost" and kinds.count("RunningCost") >= 3 and kinds[-1] == "FinalCost"
    first = sim.cp.screen[0][1]
    # ~0 (the sped-up simulator may already have sent a reading by the time it goes out)
    assert first["cost"] <= 0.05 and first["state"] == "Charging" and first["chargingPrice"]["kWhPrice"] == 0.45
    costs = [d["cost"] for m, d in sim.cp.screen if m == "RunningCost"]
    assert costs == sorted(costs) and costs[-1] > 0
    final = sim.cp.screen[-1][1]
    s = db.session(sid)
    assert final["cost"] == float(s["amount_charged"]) and final["transactionId"] == db.transaction_for_session(sid)["id"]
    assert final["qrCodeText"] == f"https://ev.example.org/pay/session/{sid}"
    assert "kWh, total £" in final["priceText"]
    sim.close()
    c.__exit__(None, None, None)


def test_next_price_change_is_announced(tmp_path, fake):
    from evcharge.tariff import Tariff
    app, c, sim = start(tmp_path, fake, tariff=Tariff.parse(
        "offpeak 00:00-16:00 0.27; peak 16:00-19:00 0.46; offpeak 19:00-24:00 0.27", session_fee=Decimal("0.30")))
    wait_for(lambda: sim.cp.config["DefaultPrice"] != "")
    assert '"priceText": "Off-peak 27p/kWh, Peak 46p/kWh 16:00-19:00, +30p per charge"' in sim.cp.config["DefaultPrice"]
    sid = pay(c)
    wait_for(lambda: len(sim.cp.screen) >= 2)
    first = sim.cp.screen[0][1]
    assert first["chargingPrice"]["flatFee"] == 0.30
    assert "nextPeriod" in first and first["triggerMeterValue"]["atTime"] == first["nextPeriod"]["atTime"]
    assert first["nextPeriod"]["chargingPrice"]["kWhPrice"] in (0.27, 0.46)
    post(c, f"/admin/sessions/{sid}/stop", {})
    sim.close()
    c.__exit__(None, None, None)


def test_free_charge_shows_no_cost(tmp_path, fake):
    app, c, sim = start(tmp_path, fake)
    post(c, "/admin/authorise", {"socket": "CP1", "amount": "1", "unit": "kwh", "note": "x"})
    db = app.state.payments.db
    sid = db.recent_sessions(1)[0]["id"]
    wait_for(lambda: db.session(sid)["status"] == "COMPLETED", timeout=20)
    wait_for(lambda: any(m == "FinalCost" for m, _ in sim.cp.screen))
    assert all(d["cost"] == 0 for _, d in sim.cp.screen)
    assert all(d["chargingPrice"]["kWhPrice"] == 0 for m, d in sim.cp.screen if m == "RunningCost")
    assert "No charge" in sim.cp.screen[-1][1]["priceText"]
    sim.close()
    c.__exit__(None, None, None)


@pytest.mark.parametrize("screen,setting", [(False, "auto"), (True, "off")])
def test_no_price_messages_unless_supported_and_wanted(tmp_path, fake, screen, setting):
    app, c, sim = start(tmp_path, fake, screen=screen, display_prices=setting)
    sim.cp.unplug_after_wh = 1000
    sid = pay(c)
    wait_for(lambda: app.state.payments.db.session(sid)["status"] == "CAPTURED")
    assert sim.cp.screen == [] and not app.state.csms.display.get("CP1")
    if screen:
        assert sim.cp.config["CustomDisplayCostAndPrice"] == "false"     # left alone when switched off
    sim.close()
    c.__exit__(None, None, None)


def test_admin_charger_page(tmp_path, fake):
    app, c, sim = start(tmp_path, fake)
    page = c.get("/admin/chargers/CP1", headers=AUTH).text
    assert "Simulator" in page and "online" in page and "NetworkType" in page and "WifiSignalStrength" in page
    assert "Read settings from the charger" in page and "Prices on its screen</th><td>yes" in page
    sim.cp.config["WifiSignalStrength"] = "-48"
    r = post(c, "/admin/chargers/CP1/read", {})
    assert "Read" in r.message and "settings" in r.message
    assert "-48" in c.get("/admin/chargers/CP1", headers=AUTH).text
    assert 'href="/admin/chargers/CP1"' in c.get("/admin", headers=AUTH).text
    off = c.get("/admin/chargers/CP2", headers=AUTH).text
    assert "offline" in off and "Read settings from the charger" not in off
    assert c.get("/admin/chargers/NOPE", headers=AUTH).status_code == 404
    assert c.get("/admin/chargers/CP1").status_code == 401
    sim.close()
    c.__exit__(None, None, None)
