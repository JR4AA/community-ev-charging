"""Watch the TLS certificate the charger actually meets.

The charger connects over wss:// to a TLS terminator (stunnel) holding a
Let's Encrypt certificate. Two things can go wrong quietly:

- the certificate isn't renewed (expired API token, broken DNS-01) and lapses;
- it IS renewed, but the terminator keeps serving the old one because nothing
  reloaded it.

Reading the file on disk would miss the second. So this connects to the
endpoint and inspects the certificate actually served, which catches both.

The result goes into /api/status for Home Assistant to alarm on, and is cached
so a 30-second poll doesn't mean a TLS handshake every 30 seconds.
"""
from __future__ import annotations

import logging
import socket
import ssl
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Cached result and when it was taken. Successes are cheap to keep for an hour;
# failures expire sooner so recovery shows up quickly.
_cache: tuple[float, dict] | None = None
OK_TTL = 3600.0
FAIL_TTL = 300.0
TIMEOUT = 5.0


def _expiry(not_after: str) -> datetime:
    """Parse OpenSSL's notAfter, e.g. 'Feb 12 09:05:55 2027 GMT'."""
    return datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)


def _issuer(cert: dict) -> str:
    for part in cert.get("issuer", ()):
        for key, value in part:
            if key == "organizationName":
                return value
    return ""


def probe(addr: str, port: int, name: str, timeout: float = TIMEOUT) -> dict:
    """Connect and report on the certificate served. Never raises.

    `name` is the hostname the certificate must be valid for, which may differ
    from `addr`: the endpoint resolves only on the church LAN, so we dial the
    address and still verify the name.
    """
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((addr, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=name) as tls:
                cert = tls.getpeercert()
        expires = _expiry(cert["notAfter"])
        left = expires - datetime.now(timezone.utc)
        return {
            "ok": True,
            "name": name,
            "expires": expires.isoformat(),
            "days_left": int(left.total_seconds() // 86400),
            "issuer": _issuer(cert),
        }
    except Exception as exc:  # noqa: BLE001 - a status endpoint must not fail
        # Covers expiry, an untrusted chain, a wrong name and the endpoint
        # being down. All of them are things Home Assistant should shout about.
        log.warning("TLS check of %s (%s:%s) failed: %s", name, addr, port, exc)
        return {"ok": False, "name": name, "error": f"{type(exc).__name__}: {exc}"}


def status(settings, now: float | None = None) -> dict | None:
    """Cached certificate status, or None when no endpoint is configured."""
    global _cache
    if not settings.tls_check_name:
        return None
    now = time.monotonic() if now is None else now
    if _cache:
        taken, result = _cache
        ttl = OK_TTL if result.get("ok") else FAIL_TTL
        if now - taken < ttl:
            return result
    result = probe(settings.tls_check_addr, settings.tls_check_port, settings.tls_check_name)
    _cache = (now, result)
    return result


def reset_cache() -> None:
    """For tests."""
    global _cache
    _cache = None
