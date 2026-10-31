from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from evcharge.config import Settings, from_env
from evcharge.db import DB
from evcharge.payments import PaymentError, Payments
from evcharge.tariff import Tariff
from evcharge.paypal import PayPal
from evcharge.web import create_app


class FakePayPal:
    """Enough of the PayPal REST API to drive the flows, with idempotent request ids."""

    def __init__(self):
        self.orders = {}
        self.auths = {}
        self.calls = []
        self.seen_request_ids = {}
        self.fail_next = {}       # path fragment -> (status, body)
        self.auth_status = "CREATED"
        self.capture_status = "COMPLETED"
        self.verify = "SUCCESS"
        self.tokens = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        self.calls.append((req.method, path))
        if path == "/v1/oauth2/token":
            self.tokens += 1
            return httpx.Response(200, json={"access_token": f"tok{self.tokens}", "expires_in": 32400})
        assert req.headers["authorization"].startswith("Bearer tok")
        for frag, (status, body) in list(self.fail_next.items()):
            if frag in path:
                del self.fail_next[frag]
                return httpx.Response(status, json=body)
        rid = req.headers.get("paypal-request-id")
        if rid and rid in self.seen_request_ids:
            return self.seen_request_ids[rid]
        resp = self._handle(req, path, json.loads(req.content) if req.content else None)
        if rid:
            self.seen_request_ids[rid] = resp
        return resp

    def _handle(self, req, path, body):
        if path == "/v2/checkout/orders":
            oid = f"ORDER{len(self.orders) + 1:08d}"
            self.orders[oid] = body
            return httpx.Response(201, json={"id": oid, "status": "CREATED"})
        if path.endswith("/authorize"):
            oid = path.split("/")[4]
            aid = f"AUTH{len(self.auths) + 1:08d}"
            amount = self.orders[oid]["purchase_units"][0]["amount"]
            self.auths[aid] = {"status": self.auth_status, "amount": amount, "captured": None}
            return httpx.Response(201, json={"id": oid, "status": "COMPLETED", "purchase_units": [{"payments": {
                "authorizations": [{"id": aid, "status": self.auth_status, "amount": amount,
                                    "expiration_time": "2026-11-27T00:00:00Z"}]}}]})
        if path.endswith("/capture"):
            aid = path.split("/")[4]
            a = self.auths[aid]
            assert a["status"] == "CREATED", "capture on a non-open authorization"
            assert Decimal(body["amount"]["value"]) <= Decimal(a["amount"]["value"])
            assert body["final_capture"] is True
            a["status"], a["captured"] = "CAPTURED", body
            gross = Decimal(body["amount"]["value"])
            fee = (gross * Decimal("0.012") + Decimal("0.30")).quantize(Decimal("0.01"))
            return httpx.Response(201, json={"id": f"CAP{aid}", "status": self.capture_status,
                                             "invoice_id": body["invoice_id"],
                                             "seller_receivable_breakdown": {
                                                 "gross_amount": body["amount"],
                                                 "paypal_fee": {"currency_code": "GBP", "value": f"{fee}"},
                                                 "net_amount": {"currency_code": "GBP", "value": f"{gross - fee}"}}})
        if path.endswith("/void"):
            aid = path.split("/")[4]
            assert self.auths[aid]["status"] == "CREATED"
            self.auths[aid]["status"] = "VOIDED"
            return httpx.Response(204)
        if path == "/v1/notifications/verify-webhook-signature":
            return httpx.Response(200, json={"verification_status": self.verify})
        return httpx.Response(404, json={"name": "NOT_FOUND"})


SETTINGS = dict(paypal_client_id="cid", paypal_client_secret="secret", paypal_webhook_id="WH1", ocpp_port=0,
                ocpp_allow_ws=frozenset({"CP1", "CP2"}),    # the simulator connects over plain ws://
                tariff=Tariff.flat(Decimal("0.45")), hold_options=(Decimal("20.00"), Decimal("50.00")))


@pytest.fixture
def fake():
    return FakePayPal()


@pytest.fixture
def pay(tmp_path, fake):
    s = Settings(db_path=str(tmp_path / "t.db"), **SETTINGS)
    return Payments(s, DB(s.db_path), PayPal(s, transport=httpx.MockTransport(fake)))


def authorised(pay, connector="CP1"):
    sid, oid = pay.create_order(connector)
    pay.authorize(oid)
    return sid, oid


def test_cost_rounds_half_up_and_caps(pay):
    sid, _ = authorised(pay)
    t0 = datetime(2026, 11, 2, 10, tzinfo=timezone.utc)
    cost = lambda wh: pay.running_cost(sid, [(t0, 0), (t0 + timedelta(minutes=30), wh)])[0]
    assert cost(1000) == Decimal("0.45")
    assert cost(11) == Decimal("0.00")      # 0.00495
    assert cost(12) == Decimal("0.01")      # 0.0054
    assert cost(100_000) == Decimal("20")   # capped at the hold


def test_kwh_for():
    s = Settings(**SETTINGS)
    assert (s.kwh_for(Decimal("20"), Decimal("0.45")), s.kwh_for(Decimal("50"), Decimal("0.45"))) == (44, 111)


def test_driver_chooses_hold(pay, fake):
    sid, oid = pay.create_order("CP1", Decimal("50"))
    assert fake.orders[oid]["purchase_units"][0]["amount"]["value"] == "50.00"
    pay.authorize(oid)
    assert pay.settle(sid, 100_000)["amount_charged"] == "45.00"     # 100 kWh fits under £50
    with pytest.raises(PaymentError) as e:
        pay.create_order("CP2", Decimal("1000"))
    assert e.value.code == 400


def test_order_is_for_the_hold_with_no_shipping(pay, fake):
    sid, oid = pay.create_order("CP1")
    order = fake.orders[oid]
    assert order["intent"] == "AUTHORIZE"
    assert order["purchase_units"][0]["amount"] == {"currency_code": "GBP", "value": "20.00"}
    assert order["purchase_units"][0]["custom_id"] == sid
    assert order["application_context"]["shipping_preference"] == "NO_SHIPPING"
    assert pay.db.session(sid)["status"] == "CREATED"


def test_full_charge_captures_cost_and_releases_rest(pay, fake):
    sid, _ = authorised(pay)
    pay.mark_charging(sid)
    s = pay.settle(sid, 12_345)   # 12.345 kWh × 0.45 = 5.555 → 5.56
    assert (s["status"], s["amount_charged"], s["energy_wh"]) == ("CAPTURED", "5.56", 12_345)
    cap = fake.auths[s["paypal_authorization_id"]]["captured"]
    assert cap["amount"]["value"] == "5.56" and cap["invoice_id"] == f"EV-{sid}"


def test_settle_twice_does_not_capture_twice(pay, fake):
    sid, _ = authorised(pay)
    pay.settle(sid, 1000)
    pay.settle(sid, 1000)
    assert sum(1 for m, p in fake.calls if p.endswith("/capture")) == 1


def test_no_energy_voids(pay, fake):
    sid, _ = authorised(pay)
    s = pay.settle(sid, 0)
    assert (s["status"], s["amount_charged"]) == ("VOIDED", "0.00")
    assert fake.auths[s["paypal_authorization_id"]]["status"] == "VOIDED"


def test_cost_never_exceeds_hold(pay, fake):
    sid, _ = authorised(pay)
    assert pay.settle(sid, 999_999)["amount_charged"] == "20.00"


def test_socket_busy_blocks_second_driver(pay):
    authorised(pay, "CP1")
    with pytest.raises(PaymentError) as e:
        pay.create_order("CP1")
    assert e.value.code == 409
    pay.create_order("CP2")   # the other socket is free


def test_race_to_authorise_second_gets_no_hold(pay, fake):
    sid1, oid1 = pay.create_order("CP1")
    sid2, oid2 = pay.create_order("CP1")   # both loaded the page while free
    pay.authorize(oid1)
    with pytest.raises(PaymentError) as e:
        pay.authorize(oid2)
    assert e.value.code == 409
    assert pay.db.session(sid2)["status"] == "ABANDONED"
    assert len(fake.auths) == 1


def test_authorise_twice_is_harmless(pay, fake):
    sid, oid = pay.create_order("CP1")
    pay.authorize(oid)
    assert pay.authorize(oid)["status"] == "AUTHORISED"
    assert len(fake.auths) == 1


def test_declined_card_keeps_order_open_for_restart(pay, fake):
    sid, oid = pay.create_order("CP1")
    fake.fail_next["/authorize"] = (422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "INSTRUMENT_DECLINED"}]})
    with pytest.raises(PaymentError) as e:
        pay.authorize(oid)
    assert (e.value.code, e.value.paypal_name) == (402, "INSTRUMENT_DECLINED")
    assert pay.db.session(sid)["status"] == "CREATED"
    assert pay.authorize(oid)["status"] == "AUTHORISED"


def test_pending_authorisation_is_refused(pay, fake):
    fake.auth_status = "PENDING"
    sid, oid = pay.create_order("CP1")
    with pytest.raises(PaymentError):
        pay.authorize(oid)
    assert pay.db.session(sid)["status"] == "FAILED"
    assert not pay.connector_busy("CP1")


def test_capture_5xx_is_retryable(pay, fake):
    sid, _ = authorised(pay)
    fake.fail_next["/capture"] = (503, {"name": "SERVICE_UNAVAILABLE"})
    with pytest.raises(PaymentError):
        pay.settle(sid, 2000)
    assert pay.db.session(sid)["status"] == "AUTHORISED"
    assert pay.settle(sid, 2000)["status"] == "CAPTURED"


def test_capture_4xx_is_final(pay, fake):
    sid, _ = authorised(pay)
    fake.fail_next["/capture"] = (422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "AUTHORIZATION_EXPIRED"}]})
    with pytest.raises(PaymentError):
        pay.settle(sid, 2000)
    assert pay.db.session(sid)["status"] == "FAILED"


def test_capture_pending_then_webhook_completes(pay, fake):
    fake.capture_status = "PENDING"
    sid, _ = authorised(pay)
    s = pay.settle(sid, 2000)
    assert s["status"] == "CAPTURE_PENDING"
    ev = {"id": "WH-1", "event_type": "PAYMENT.CAPTURE.COMPLETED",
          "resource": {"id": s["paypal_capture_id"], "status": "COMPLETED", "invoice_id": f"EV-{sid}"}}
    assert pay.handle_webhook(ev) == "PAYMENT.CAPTURE.COMPLETED"
    assert pay.db.session(sid)["status"] == "CAPTURED"
    assert pay.handle_webhook(ev) == "duplicate"


def test_expired_token_is_refreshed(pay, fake):
    pay.create_order("CP1")
    fake.fail_next["/v2/checkout/orders"] = (401, {"name": "AUTHENTICATION_FAILURE"})
    pay.create_order("CP2")
    assert fake.tokens == 2


def test_unused_hold_released_and_unapproved_abandoned(pay, fake):
    sid, _ = authorised(pay, "CP1")
    sid2, _ = pay.create_order("CP2")
    later = datetime.now(timezone.utc) + timedelta(minutes=16)
    pay.expire_unused(later)
    assert pay.db.session(sid)["status"] == "VOIDED"
    assert pay.db.session(sid2)["status"] == "CREATED"
    pay.expire_unused(later + timedelta(hours=3))
    assert pay.db.session(sid2)["status"] == "ABANDONED"


def test_charging_session_not_expired(pay):
    sid, _ = authorised(pay)
    pay.mark_charging(sid)
    pay.expire_unused(datetime.now(timezone.utc) + timedelta(hours=10))
    assert pay.db.session(sid)["status"] == "CHARGING"


def test_voided_at_paypal_frees_socket(pay):
    sid, _ = authorised(pay)
    aid = pay.db.session(sid)["paypal_authorization_id"]
    pay.handle_webhook({"id": "WH-2", "event_type": "PAYMENT.AUTHORIZATION.VOIDED", "resource": {"id": aid}})
    assert pay.db.session(sid)["status"] == "VOIDED"
    assert not pay.connector_busy("CP1")


def test_settings_from_env():
    s = from_env({"PAYPAL_ENV": "live", "PRICE_PER_KWH": "0.50", "HOLD_OPTIONS": "25, 60", "CONNECTORS": "A, B"})
    assert s.paypal_api == "https://api-m.paypal.com"
    assert (s.tariff.max_price, s.hold_options, s.connectors) == (Decimal("0.50"), (Decimal("25"), Decimal("60")), ("A", "B"))
    assert from_env({"HOLD_AMOUNT": "30"}).hold_options == (Decimal("30.00"),)   # older single-hold setting
    assert from_env({}).hold_options == (Decimal("20.00"), Decimal("50.00"))
    with pytest.raises(ValueError):
        from_env({"PAYPAL_ENV": "production"})
    with pytest.raises(ValueError):
        from_env({"HOLD_OPTIONS": "20,0"})


# -- web --------------------------------------------------------------------

class StubCharger:
    """Stands in for a connected charger that accepts every command (see test_ocpp for the real thing)."""

    async def call(self, request):
        return SimpleNamespace(status="Accepted")


def plug_in(app, socket="CP1"):
    cp, conn = app.state.csms.settings.sockets[socket]
    app.state.csms.points[cp] = StubCharger()
    app.state.csms.status[(cp, conn)] = "Preparing"


@pytest.fixture
def client(tmp_path, fake):
    s = Settings(db_path=str(tmp_path / "w.db"), **SETTINGS)
    app = create_app(s, PayPal(s, transport=httpx.MockTransport(fake)))
    with TestClient(app) as c:
        plug_in(app, "CP1")
        plug_in(app, "CP2")
        yield c


def test_web_flow(client, fake):
    assert client.get("/pay/health").text == "ok\n"
    page = client.get("/pay/CP1")
    assert page.status_code == 200 and "intent=authorize" in page.text and "client-id=cid" in page.text
    assert 'value="20.00" checked' in page.text and 'value="50.00"' in page.text and "111 kWh" in page.text
    assert client.post("/pay/api/orders", json={"connector": "CP2", "hold": "999"}).status_code == 400
    oid = client.post("/pay/api/orders", json={"connector": "CP1", "hold": "50.00"}).json()["orderID"]
    r = client.post(f"/pay/api/orders/{oid}/authorize").json()
    assert r["status"] == "AUTHORISED"
    status = client.get(f"/pay/session/{r['session']}")
    assert "Starting the charger" in status.text and "£50.00" in status.text
    assert "in use" in client.get("/pay/CP1").text
    assert client.post("/pay/api/orders", json={"connector": "CP1"}).status_code == 409


def test_web_rejects_unknown(client):
    assert client.get("/pay/NOPE").status_code == 404
    assert client.post("/pay/api/orders", json={"connector": "NOPE"}).status_code == 404
    assert client.get("/pay/session/short").status_code == 404
    assert client.post("/pay/api/orders/bad-id/authorize").status_code == 404
    for path in ("/docs", "/openapi.json", "/redoc"):
        assert client.get(path).status_code == 404


def test_web_not_configured(tmp_path):
    s = Settings(db_path=str(tmp_path / "n.db"), ocpp_port=0)
    with TestClient(create_app(s)) as c:
        assert "not set up yet" in c.get("/pay/CP1").text
        assert c.post("/pay/api/orders", json={"connector": "CP1"}).status_code == 503


def test_webhook_verification(client, fake):
    ev = {"id": "WH-9", "event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {"id": "X"}}
    headers = {"PAYPAL-AUTH-ALGO": "SHA256withRSA", "PAYPAL-CERT-URL": "https://api.paypal.com/c",
               "PAYPAL-TRANSMISSION-ID": "t", "PAYPAL-TRANSMISSION-SIG": "s", "PAYPAL-TRANSMISSION-TIME": "now"}
    assert client.post("/webhooks/paypal", json=ev).status_code == 400            # no signature headers
    fake.verify = "FAILURE"
    assert client.post("/webhooks/paypal", json=ev, headers=headers).status_code == 400
    fake.verify = "SUCCESS"
    assert client.post("/webhooks/paypal", json=ev, headers=headers).status_code == 200


# -- time-of-use ---------------------------------------------------------------

CHURCH = "offpeak 00:00-16:00 0.2626; peak 16:00-19:00 0.4524; offpeak 19:00-24:00 0.2626"


@pytest.fixture
def tou(tmp_path, fake):
    s = Settings(db_path=str(tmp_path / "tou.db"), **{**SETTINGS, "tariff": Tariff.parse(CHURCH, Decimal("0.05"))})
    return Payments(s, DB(s.db_path), PayPal(s, transport=httpx.MockTransport(fake)))


def test_charge_across_peak_priced_by_reading_time(tou, fake):
    # 15:00-17:00 GMT on a November day, 7 kW: 7 kWh off-peak then 7 kWh peak.
    sid, _ = authorised(tou)
    t = datetime(2026, 11, 2, 15, tzinfo=timezone.utc)
    readings = [(t + timedelta(minutes=m), 7000 * m // 60) for m in range(0, 121, 10)]
    s = tou.settle(sid, readings=readings)
    # 7 × 0.2626 = 1.8382, 7 × 0.4524 = 3.1668, total 5.005 → 5.01
    assert (s["status"], s["amount_charged"], s["energy_wh"]) == ("CAPTURED", "5.01", 14000)
    b = json.loads(s["breakdown"])
    assert b == {"offpeak": {"wh": 7000, "price": "0.2626", "cost": "1.84"},
                 "peak": {"wh": 7000, "price": "0.4524", "cost": "3.17"}}


def test_solar_period_overrides_band(tou):
    sid, _ = authorised(tou)
    t = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    tou.db.set_solar(True, (t + timedelta(minutes=30)).isoformat(timespec="seconds"))
    tou.db.set_solar(False, (t + timedelta(minutes=90)).isoformat(timespec="seconds"))
    cost, b = tou.running_cost(sid, [(t, 0), (t + timedelta(hours=2), 8000)])
    # 4 kWh solar at 5p + 4 kWh off-peak at 26.26p = 0.20 + 1.0504
    assert cost == Decimal("1.25")
    assert (b["solar"]["wh"], b["offpeak"]["wh"]) == (4000, 4000)


def test_solar_still_on_counts_to_now(tou):
    sid, _ = authorised(tou)
    t = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    tou.db.set_solar(True, t.isoformat(timespec="seconds"))
    assert tou.db.set_solar(True) is False       # already on
    cost, b = tou.running_cost(sid, [(t, 0), (t + timedelta(hours=1), 10_000)])
    assert (cost, list(b)) == (Decimal("0.50"), ["solar"])


def test_peak_is_local_time_in_summer(tou):
    # 15:30 UTC in July is 16:30 BST: peak.
    sid, _ = authorised(tou)
    t = datetime(2026, 7, 1, 15, 30, tzinfo=timezone.utc)
    _, b = tou.running_cost(sid, [(t, 0), (t + timedelta(minutes=20), 1000)])
    assert list(b) == ["peak"]


def test_session_keeps_the_tariff_it_started_with(tou, tmp_path, fake):
    sid, _ = authorised(tou)
    cheaper = Settings(db_path=tou.settings.db_path, **{**SETTINGS, "tariff": Tariff.flat(Decimal("0.01"))})
    later = Payments(cheaper, DB(cheaper.db_path), tou.paypal)
    t = datetime(2026, 11, 2, 17, tzinfo=timezone.utc)
    assert later.running_cost(sid, [(t, 0), (t + timedelta(minutes=30), 1000)])[0] == Decimal("0.45")


def test_kwh_for_each_rate(tou):
    assert (tou.settings.kwh_for(Decimal("20"), Decimal("0.4524")), tou.settings.kwh_for(Decimal("20"), Decimal("0.2626"))) == (44, 76)


def test_tariff_parse_rejects_gaps():
    with pytest.raises(ValueError):
        Tariff.parse("offpeak 00:00-16:00 0.26; peak 17:00-24:00 0.45")
    with pytest.raises(ValueError):
        Tariff.parse("offpeak 00:00-16:00 0.26")
    with pytest.raises(ValueError):
        Tariff.parse("peak 16-19 0.45")


def test_tariff_from_env():
    s = from_env({"TARIFF": CHURCH, "SOLAR_PRICE_PER_KWH": "0.05"})
    assert [(n, p) for n, p, _ in s.tariff.rates()] == [("offpeak", Decimal("0.2626")), ("peak", Decimal("0.4524"))]
    assert s.tariff.solar_price == Decimal("0.05")
    assert Tariff.from_json(s.tariff.to_json()) == s.tariff


def test_pay_page_shows_rates(tmp_path, fake):
    s = Settings(db_path=str(tmp_path / "p.db"), **{**SETTINGS, "tariff": Tariff.parse(CHURCH, Decimal("0.05"))})
    app = create_app(s, PayPal(s, transport=httpx.MockTransport(fake)))
    with TestClient(app) as c:
        plug_in(app)
        page = c.get("/pay/CP1").text
        assert "Off-peak" in page and "26.26p" in page and "45.24p" in page and "16:00–19:00" in page
        assert page.index("Peak") < page.index("Off-peak") and "00:00–16:00" not in page
        assert "5p per kWh" in page and "← now" in page
        assert "<th>Peak</th><th>Off-peak</th>" in page and "<td>44 kWh</td><td>76 kWh</td>" in page
        assert "<th>Amount to hold</th><th colspan=2>Estimated max charge</th>" in page


def test_session_fee(tmp_path, fake):
    t = Tariff.parse(CHURCH, Decimal("0.05"), Decimal("0.30"))
    s = Settings(db_path=str(tmp_path / "fee.db"), **{**SETTINGS, "tariff": t})
    pay = Payments(s, DB(s.db_path), PayPal(s, transport=httpx.MockTransport(fake)))
    sid, _ = authorised(pay)
    t0 = datetime(2026, 11, 2, 10, tzinfo=timezone.utc)
    cost, b = pay.running_cost(sid, [(t0, 0), (t0 + timedelta(hours=1), 10_000)])
    assert cost == Decimal("2.93") and b["fee"]["cost"] == "0.30"    # 10 × 0.2626 = 2.626 + 0.30
    assert pay.running_cost(sid, [(t0, 0), (t0 + timedelta(hours=1), 0)])[0] == 0   # no energy, no fee
    assert pay.settle(sid, 0)["status"] == "VOIDED"
    assert s.kwh_for(Decimal("20"), Decimal("0.4524")) == 43          # (20 - 0.30) / 0.4524 = 43.5
    assert Tariff.from_json(t.to_json()).session_fee == Decimal("0.30")
    assert from_env({"TARIFF": CHURCH, "SESSION_FEE": "0.30"}).tariff.session_fee == Decimal("0.30")
    assert "(the PayPal fee)" in __import__("evcharge.web", fromlist=["x"])._rates_table(t, False, "offpeak")
    assert "30p per charge" in __import__("evcharge.web", fromlist=["x"])._rates_table(t, False, "offpeak")


def test_upgrades_a_first_release_database(tmp_path):
    """The sessions table as first deployed (before tariff, breakdown, id_tag)."""
    import sqlite3
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.executescript("""CREATE TABLE sessions (id TEXT PRIMARY KEY, connector TEXT NOT NULL, status TEXT NOT NULL,
        hold_amount TEXT NOT NULL, price_per_kwh TEXT NOT NULL, paypal_order_id TEXT UNIQUE,
        paypal_authorization_id TEXT, authorization_expires TEXT, paypal_capture_id TEXT, energy_wh INTEGER,
        amount_charged TEXT, error TEXT, created_at TEXT NOT NULL, authorized_at TEXT, started_at TEXT,
        finished_at TEXT, updated_at TEXT NOT NULL);
        INSERT INTO sessions (id, connector, status, hold_amount, price_per_kwh, created_at, updated_at)
        VALUES ('old-session-0000000', 'CP1', 'CAPTURED', '20.00', '0.45', '2026-10-30', '2026-10-30');""")
    c.close()
    db = DB(path)
    s = db.session("old-session-0000000")
    assert s["status"] == "CAPTURED" and s["id_tag"] is None and s["tariff"] is None
    DB(path)   # and again, already migrated


def test_card_only_setting(tmp_path, fake):
    for methods, card in (("card", True), ("all", False)):
        s = Settings(db_path=str(tmp_path / f"{methods}.db"), **{**SETTINGS, "payment_methods": methods})
        app = create_app(s, PayPal(s, transport=httpx.MockTransport(fake)))
        with TestClient(app) as c:
            plug_in(app)
            page = c.get("/pay/CP1").text
            assert ("const card = true" in page) is card
            assert ("Pay by debit or credit card." in page) is card
            assert "paypal.Buttons(handlers)" in page        # fallback always present
    with pytest.raises(ValueError):
        from_env({"PAYMENT_METHODS": "cash"})


def test_hold_default(tmp_path, fake):
    s = from_env({"HOLD_OPTIONS": "60,40,20", "HOLD_DEFAULT": "40"})
    assert (s.hold_options, s.hold_default) == ((Decimal("60.00"), Decimal("40.00"), Decimal("20.00")), Decimal("40"))
    assert from_env({"HOLD_OPTIONS": "60,40,20"}).hold_default == Decimal("60.00")
    with pytest.raises(ValueError):
        from_env({"HOLD_OPTIONS": "60,40,20", "HOLD_DEFAULT": "50"})
    s = Settings(db_path=str(tmp_path / "hd.db"), **{**SETTINGS, "hold_options": s.hold_options, "hold_default": Decimal("40")})
    app = create_app(s, PayPal(s, transport=httpx.MockTransport(fake)))
    with TestClient(app) as c:
        plug_in(app)
        page = c.get("/pay/CP1").text
        assert 'value="40.00" checked' in page and 'value="60.00" checked' not in page
        assert page.index('value="60.00"') < page.index('value="40.00"')
        oid = c.post("/pay/api/orders", json={"connector": "CP1"}).json()["orderID"]
        assert fake.orders[oid]["purchase_units"][0]["amount"]["value"] == "40.00"


def test_power_label_per_socket(tmp_path, fake):
    s = from_env({"SOCKET_KW_CP1": "22", "SOCKET_SHARES_CP1": "CP2", "SOCKET_KW_CP2": "7"})
    assert (s.power_label("CP1"), s.power_label("CP2")) == ("22 kW (shared with CP2)", "7 kW")
    assert from_env({}).power_label("CP1") == ""
    with pytest.raises(ValueError):
        from_env({"SOCKET_KW_NOPE": "22"})
    s = Settings(db_path=str(tmp_path / "pl.db"), **{**SETTINGS, "socket_kw": {"CP1": 22},
                                                     "socket_shares": {"CP1": ("CP2",)}})
    app = create_app(s, PayPal(s, transport=httpx.MockTransport(fake)))
    with TestClient(app) as c:
        assert "<h1>Socket CP1 <span class=power>22 kW (shared with CP2)</span></h1>" in c.get("/pay/CP1").text
        assert "<span class=power>" not in c.get("/pay/CP2").text


def test_branding(tmp_path, fake):
    brand = tmp_path / "brand"
    brand.mkdir()
    (brand / "logo.png").write_bytes(b"\x89PNG fake")
    (brand / "secret.txt").write_text("no")
    s = Settings(db_path=str(tmp_path / "b.db"), **{**SETTINGS, "brand_name": "Example Church",
                 "brand_logo": "logo.png", "brand_dir": str(brand), "brand_color": "#7b0303",
                 "footer_text": "Example Church, charity 123.", "page_note": "First <p>.\\n\\nSecond."})
    app = create_app(s, PayPal(s, transport=httpx.MockTransport(fake)))
    with TestClient(app) as c:
        page = c.get("/pay/CP1").text
        assert '<header class=brand><img src="/static/brand/logo.png" alt=""><span>Example Church</span></header>' in page
        assert '<link rel="icon" href="/static/brand/logo.png">' in page and "--accent: #7b0303" in page
        assert page.index("<p class=note>First &lt;p&gt;.</p><p class=note>Second.</p>") < page.index("charity 123.")
        assert c.get("/static/brand/logo.png").content == b"\x89PNG fake"
        for bad in ("secret.txt", "..%2Fb.db", "nope.png"):
            assert c.get(f"/static/brand/{bad}").status_code == 404


def test_unbranded_defaults(tmp_path):
    s = Settings(db_path=str(tmp_path / "u.db"), ocpp_port=0)
    with TestClient(create_app(s)) as c:
        page = c.get("/pay/CP1").text
        assert "<header" not in page and "charity" not in page and "<link rel=\"icon\"" not in page
        assert c.get("/static/brand/anything.png").status_code == 404
    for bad in ({"BRAND_COLOR": "red"}, {"BRAND_LOGO": "../etc/passwd"}):
        with pytest.raises(ValueError):
            from_env(bad)


def test_paypal_brand_name_only_when_set(tmp_path, fake):
    for name in ("", "Example Church"):
        s = Settings(db_path=str(tmp_path / f"pb{len(name)}.db"), **{**SETTINGS, "brand_name": name})
        pay = Payments(s, DB(s.db_path), PayPal(s, transport=httpx.MockTransport(fake)))
        _, oid = pay.create_order("CP1")
        assert fake.orders[oid]["application_context"].get("brand_name") == (name or None)



def test_support_contact_and_invoice_prefix(tmp_path, fake):
    s = Settings(db_path=str(tmp_path / "sc.db"), **{**SETTINGS, "invoice_prefix": "ABC-",
                 "support_contact": "Please call 01234 567890."})
    app = create_app(s, PayPal(s, transport=httpx.MockTransport(fake)))
    pay = app.state.payments
    sid, _ = authorised(pay)
    fake.fail_next["/capture"] = (422, {"name": "UNPROCESSABLE_ENTITY"})
    with pytest.raises(PaymentError):
        pay.settle(sid, 1000)
    with TestClient(app) as c:
        assert "Please call 01234 567890." in c.get(f"/pay/session/{sid}").text
    sid2, _ = authorised(pay, "CP2")
    s2 = pay.settle(sid2, 1000)
    assert fake.auths[s2["paypal_authorization_id"]]["captured"]["invoice_id"] == f"ABC-{sid2}"
    assert from_env({}).invoice_prefix == "EV-"



def test_index_shows_sockets_and_prices(client):
    page = client.get("/").text
    assert 'href="/pay/CP1"' in page and "car plugged in" in page        # the client fixture plugs both in
    assert "Prices" in page and "45p per kWh" in page and "PayPal holds an amount" in page
    assert "You can hold £20, £50." in page and "← All sockets" not in page
    assert '<a href="/">← All sockets and prices</a>' in client.get("/pay/CP1").text


def test_index_states(tmp_path):
    s = Settings(db_path=str(tmp_path / "i.db"), ocpp_port=0)
    with TestClient(create_app(s)) as c:
        assert c.get("/").text.count("offline") == 2



def test_index_note_at_top(tmp_path):
    s = Settings(db_path=str(tmp_path / "nt.db"), ocpp_port=0, page_note="Hello drivers.", footer_text="Charity 1.")
    with TestClient(create_app(s)) as c:
        home = c.get("/").text
        assert home.count("Hello drivers.") == 1 and home.index("Hello drivers.") < home.index("<h2>Sockets</h2>")
        sock = c.get("/pay/CP1").text
        assert sock.index("Hello drivers.") < sock.index("Charity 1.")      # note at the foot, charity line last



def test_session_link_in_paypal_description(tmp_path, fake):
    s = Settings(db_path=str(tmp_path / "pu.db"), **{**SETTINGS, "public_url": "https://ev.example.org"})
    pay = Payments(s, DB(s.db_path), PayPal(s, transport=httpx.MockTransport(fake)))
    sid, oid = pay.create_order("CP1")
    d = fake.orders[oid]["purchase_units"][0]["description"]
    assert d == f"EV charging, socket CP1. Progress: https://ev.example.org/pay/session/{sid}" and len(d) <= 127
    long = Settings(db_path=str(tmp_path / "pl2.db"), **{**SETTINGS, "public_url": "https://" + "x" * 120 + ".org"})
    pay2 = Payments(long, DB(long.db_path), PayPal(long, transport=httpx.MockTransport(fake)))
    _, oid2 = pay2.create_order("CP1")
    assert fake.orders[oid2]["purchase_units"][0]["description"] == "EV charging, socket CP1"
    with pytest.raises(ValueError):
        from_env({"PUBLIC_URL": "http://ev.example.org"})


def test_session_remembered_on_the_phone(client):
    oid = client.post("/pay/api/orders", json={"connector": "CP1"}).json()["orderID"]
    sid = client.post(f"/pay/api/orders/{oid}/authorize").json()["session"]
    page = client.get(f"/pay/session/{sid}").text
    assert f'localStorage.setItem("evcharge.session", "{sid}")' in page
    assert 'id=yours' in client.get("/").text and 'id=yours' in client.get("/pay/CP2").text



def test_powered_by_footer(client):
    for path in ("/", "/pay/CP1"):
        page = client.get(path).text
        assert 'Powered by <a href="https://github.com/JR4AA/community-ev-charging"' in page
        assert page.rindex("Powered by") > page.rindex("</h1>")
