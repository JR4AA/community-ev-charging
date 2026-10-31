"""Admin pages: LAN only, password, Origin check, and manual charges end to end."""
import base64
from urllib.parse import unquote
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from evcharge.config import Settings
from evcharge.paypal import PayPal
from evcharge.web import create_app
from tests.test_ocpp import Sim, wait_for
from tests.test_payments import SETTINGS, FakePayPal

AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:pw-123").decode()}
LAN = ("192.168.1.50", 50000)


@pytest.fixture
def fake():
    return FakePayPal()


def make(tmp_path, fake, **kw):
    s = Settings(db_path=str(tmp_path / "a.db"), **{**SETTINGS, "admin_password": "pw-123", **kw})
    return create_app(s, PayPal(s, transport=httpx.MockTransport(fake)))


class Posted:
    def __init__(self, r):
        self.status_code, self.message = r.status_code, unquote(r.headers.get("location", ""))


def post(c, url, data, origin="http://testserver"):
    return Posted(c.post(url, data=data, headers={**AUTH, "Origin": origin}, follow_redirects=False))


def test_admin_locks(tmp_path, fake):
    app = make(tmp_path, fake)
    with TestClient(app, client=LAN) as c:
        assert c.get("/admin").status_code == 401
        assert c.get("/admin", headers={"Authorization": "Basic " + base64.b64encode(b"admin:no").decode()}).status_code == 401
        assert c.get("/admin", headers=AUTH).status_code == 200
        assert c.get("/admin", headers={**AUTH, "Cf-Connecting-Ip": "203.0.113.1"}).status_code == 404   # via the tunnel
        assert post(c, "/admin/authorise", {"socket": "CP1", "amount": "5", "unit": "kwh"}, origin="https://evil.example").status_code == 403
        assert c.post("/admin/authorise", data={"socket": "CP1", "amount": "5", "unit": "kwh"}, headers=AUTH).status_code == 403
    with TestClient(app, client=("203.0.113.50", 50000)) as c:                                        # public address
        assert c.get("/admin", headers=AUTH).status_code == 404
    (tmp_path / "x").mkdir()
    with TestClient(make(tmp_path / "x", fake, admin_password=""), client=LAN) as c:                    # no password set
        assert c.get("/admin", headers=AUTH).status_code == 404


@pytest.mark.parametrize("unit,amount,check", [
    ("kwh", "2", lambda s: 1700 <= s["energy_wh"] <= 2300),
    ("gbp", "1.50", lambda s: s["energy_wh"] >= 1000),
])
def test_manual_charge_stops_at_limit_and_takes_no_payment(tmp_path, fake, unit, amount, check):
    (tmp_path / "x").mkdir(exist_ok=True)
    app = make(tmp_path / "x", fake, stop_margin=Decimal("0.10"), stop_margin_wh=100)
    with TestClient(app, client=LAN) as c:
        port = app.state.ocpp_server.sockets[0].getsockname()[1]
        assert "plug the car in first" in post(c, "/admin/authorise", {"socket": "CP1", "amount": amount, "unit": unit}).message
        sim = Sim(port, kw=7, tick=0.02, speed=600)
        wait_for(lambda: app.state.csms.socket_state("CP1") == "Preparing")
        r = post(c, "/admin/authorise", {"socket": "CP1", "amount": amount, "unit": unit, "note": "minister"})
        assert "Started" in r.message
        db = app.state.payments.db
        sid = db.recent_sessions(1)[0]["id"]
        wait_for(lambda: db.session(sid)["status"] == "COMPLETED", timeout=20)
        s = db.session(sid)
        assert check(s) and s["amount_charged"] == "0.00" and s["payment_method"] == "manual" and s["note"] == "minister"
        assert fake.orders == {} and fake.auths == {}                       # PayPal never involved
        page = c.get("/admin", headers=AUTH).text
        assert "free (" in page and "worth £" in page and "minister" in page
        sim.close()


def test_admin_stop_and_busy(tmp_path, fake):
    app = make(tmp_path, fake)
    with TestClient(app, client=LAN) as c:
        port = app.state.ocpp_server.sockets[0].getsockname()[1]
        sim = Sim(port, kw=7, tick=0.05, speed=60)
        wait_for(lambda: app.state.csms.socket_state("CP1") == "Preparing")
        post(c, "/admin/authorise", {"socket": "CP1", "amount": "50", "unit": "kwh"})
        db = app.state.payments.db
        sid = db.recent_sessions(1)[0]["id"]
        wait_for(lambda: db.session(sid)["status"] == "CHARGING")
        assert "isn't ready" in post(c, "/admin/authorise", {"socket": "CP1", "amount": "5", "unit": "kwh"}).message
        assert "Stop sent" in post(c, f"/admin/sessions/{sid}/stop", {}).message
        wait_for(lambda: db.session(sid)["status"] == "COMPLETED")
        sim.close()



def test_all_charges_totals_and_csv(tmp_path, fake):
    app = make(tmp_path, fake, tariff=__import__("evcharge.tariff", fromlist=["x"]).Tariff.flat(Decimal("0.45"), session_fee=Decimal("0.30")))
    pay = app.state.payments
    from tests.test_payments import authorised
    paid, _ = authorised(pay, "CP1")
    pay.settle(paid, 10_000)                                   # 10 kWh at 45p + 30p = £4.80, PayPal fee 1.2% + 30p = £0.36
    released, _ = authorised(pay, "CP2")
    pay.settle(released, 0)                                    # nothing delivered: hold released
    free = pay.authorise_manual("CP1", limit_kwh=Decimal("5"), note="minister")["id"]
    pay.settle(free, 4_000)
    pay.create_order("CP2")                                    # abandoned attempt: not a charge
    with TestClient(app, client=LAN) as c:
        page = c.get("/admin/charges", headers=AUTH).text
        assert "£4.80 taken, £0.36 PayPal fees, £4.44 net" in page
        assert "4.00 kWh" in page and "worth £1.80 at the tariff" in page
        assert page.count("<tr><td class=small>") == 3        # three charges listed, not the abandoned one
        r = c.get("/admin/charges.csv", headers=AUTH)
        assert r.headers["content-type"].startswith("text/csv")
        lines = r.text.strip().splitlines()
        assert len(lines) == 4 and lines[0].startswith("date,time,socket,status,payment,kwh,charged,paypal_fee,net")
        row = next(l for l in lines if paid in l)
        assert ",captured,paypal,10.000,4.80,0.36,4.44,4.50,0.30," in row and f"EV-{paid}" in row
        assert any("manual" in l and "minister" in l and ",1.80," in l for l in lines)
        month = lines[1][:7]
        assert month in c.get(f"/admin/charges?month={month}", headers=AUTH).text
        assert "None." in c.get("/admin/charges?month=2001-01", headers=AUTH).text



def test_api_status_for_home_assistant(tmp_path, fake):
    (tmp_path / "y").mkdir()
    app = make(tmp_path, fake, api_token="tok-abc")
    bearer = {"Authorization": "Bearer tok-abc"}
    with TestClient(app, client=LAN) as c:
        assert c.get("/api/status").status_code == 401
        assert c.get("/api/status", headers={"Authorization": "Bearer nope"}).status_code == 401
        assert c.get("/api/status", headers=AUTH).status_code == 401                 # the admin password isn't the token
        assert c.get("/api/status", headers={**bearer, "Cf-Connecting-Ip": "203.0.113.1"}).status_code == 404
        j = c.get("/api/status", headers=bearer).json()
        assert j["currency"] == "GBP"
        assert j["sockets"]["CP1"] == {"state": "Offline", "online": False, "charge_point": "CP1", "power_kw": 0.0, "session": None}
        port = app.state.ocpp_server.sockets[0].getsockname()[1]
        sim = Sim(port, kw=7, tick=0.05, speed=600)   # 58 Wh per reading: little rounding
        wait_for(lambda: app.state.csms.socket_state("CP1") == "Preparing")
        post(c, "/admin/authorise", {"socket": "CP1", "amount": "200", "unit": "kwh", "note": "test"})
        db = app.state.payments.db
        sid = db.recent_sessions(1)[0]["id"]
        wait_for(lambda: db.session(sid)["status"] == "CHARGING")
        wait_for(lambda: len(db.readings(db.transaction_for_session(sid)["id"])) >= 3)
        j = c.get("/api/status", headers=bearer).json()
        cp1 = j["sockets"]["CP1"]
        assert cp1["online"] and cp1["state"] == "Charging" and 6.5 <= cp1["power_kw"] <= 7.5
        assert cp1["session"]["kind"] == "free" and cp1["session"]["energy_kwh"] > 0 and cp1["session"]["limit_kwh"] == 200
        assert j["chargers"]["CP1"]["model"] == "Simulator" and j["chargers"]["CP1"]["last_seen"]
        post(c, f"/admin/sessions/{sid}/stop", {})
        wait_for(lambda: db.session(sid)["status"] == "COMPLETED")
        j = c.get("/api/status", headers=bearer).json()
        assert j["sockets"]["CP1"]["session"] is None and j["today"]["charges"] == 1 and j["today"]["kwh"] > 0
        sim.close()
    with TestClient(make(tmp_path / "y", fake, api_token=""), client=LAN) as c:        # no token set: no API
        assert c.get("/api/status", headers=bearer).status_code == 404



def test_api_solar_flag(tmp_path, fake):
    app = make(tmp_path, fake, api_token="tok-abc")
    bearer = {"Authorization": "Bearer tok-abc"}
    with TestClient(app, client=LAN) as c:
        assert c.post("/api/solar", json={"on": True}).status_code == 401
        assert c.post("/api/solar", json={"on": True}, headers={**bearer, "Cf-Connecting-Ip": "203.0.113.1"}).status_code == 404
        assert c.post("/api/solar", json={"on": "yes"}, headers=bearer).status_code == 400
        assert c.post("/api/solar", content=b"nonsense", headers=bearer).status_code == 400
        assert c.post("/api/solar", json={"on": True}, headers=bearer).json() == {"solar_rate_on": True, "changed": True}
        assert c.post("/api/solar", json={"on": True}, headers=bearer).json() == {"solar_rate_on": True, "changed": False}
        assert c.get("/api/status", headers=bearer).json()["solar_rate_on"] is True
        assert c.post("/api/solar", json={"on": False}, headers=bearer).json()["solar_rate_on"] is False
        assert len(app.state.payments.db.solar_periods("2000-01-01", "2100-01-01")) == 1    # one closed period recorded
