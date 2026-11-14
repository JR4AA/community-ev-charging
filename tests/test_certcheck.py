"""The TLS certificate watch that feeds /api/status."""
import socket
from datetime import datetime, timedelta, timezone

import pytest

from evcharge import certcheck
from evcharge.config import Settings, from_env


@pytest.fixture(autouse=True)
def _clear_cache():
    certcheck.reset_cache()
    yield
    certcheck.reset_cache()


def test_parses_openssl_notafter():
    assert certcheck._expiry("Feb 12 09:05:55 2027 GMT") == datetime(2027, 2, 12, 9, 5, 55, tzinfo=timezone.utc)


def test_issuer_pulled_from_cert():
    cert = {"issuer": ((("countryName", "US"),), (("organizationName", "Let's Encrypt"),))}
    assert certcheck._issuer(cert) == "Let's Encrypt"
    assert certcheck._issuer({}) == ""


def test_absent_when_not_configured():
    assert certcheck.status(Settings()) is None


def test_reports_days_left(monkeypatch):
    expires = datetime.now(timezone.utc) + timedelta(days=61, hours=2)
    monkeypatch.setattr(
        certcheck, "probe",
        lambda addr, port, name, timeout=5.0: {
            "ok": True, "name": name, "expires": expires.isoformat(),
            "days_left": 61, "issuer": "Let's Encrypt"},
    )
    s = Settings(tls_check_name="ocpp.example.org", tls_check_addr="10.0.0.1")
    out = certcheck.status(s)
    assert out["ok"] is True and out["days_left"] == 61 and out["issuer"] == "Let's Encrypt"


def test_success_is_cached_but_failure_is_retried_sooner(monkeypatch):
    calls = []

    def fake_probe(addr, port, name, timeout=5.0):
        calls.append(name)
        return {"ok": len(calls) > 1, "name": name, "days_left": 9}

    monkeypatch.setattr(certcheck, "probe", fake_probe)
    s = Settings(tls_check_name="ocpp.example.org", tls_check_addr="10.0.0.1")

    certcheck.status(s, now=0.0)                 # first call: failure, cached briefly
    certcheck.status(s, now=10.0)                # inside the failure TTL: no new probe
    assert len(calls) == 1
    certcheck.status(s, now=certcheck.FAIL_TTL + 1)   # failure TTL passed: probe again
    assert len(calls) == 2
    certcheck.status(s, now=certcheck.FAIL_TTL + 100)  # now a success, cached for longer
    assert len(calls) == 2
    certcheck.status(s, now=certcheck.FAIL_TTL + certcheck.OK_TTL + 2)
    assert len(calls) == 3


def test_unreachable_endpoint_reports_not_ok():
    # Bind and close a port so nothing is listening on it.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    out = certcheck.probe("127.0.0.1", port, "ocpp.example.org", timeout=2.0)
    assert out["ok"] is False
    assert "error" in out and out["name"] == "ocpp.example.org"


def test_env_parsing_and_addr_defaults_to_name():
    s = from_env({"TLS_CHECK": "ocpp.example.org"})
    assert s.tls_check_name == "ocpp.example.org"
    assert s.tls_check_addr == "ocpp.example.org"   # dial the name when it resolves
    assert s.tls_check_port == 443

    s = from_env({"TLS_CHECK": "ocpp.example.org", "TLS_CHECK_ADDR": "192.168.0.10", "TLS_CHECK_PORT": "9443"})
    assert (s.tls_check_addr, s.tls_check_port) == ("192.168.0.10", 9443)


def test_port_in_tls_check_is_rejected():
    with pytest.raises(ValueError, match="hostname only"):
        from_env({"TLS_CHECK": "ocpp.example.org:443"})
