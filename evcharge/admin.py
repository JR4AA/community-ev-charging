"""Admin pages: /admin, for the site's own network only.

Three locks, any one of which keeps the public out:
- the tunnel's ingress rules never forward /admin (404 at Cloudflare's edge);
- anything that did arrive through the tunnel (it carries Cf-Connecting-Ip), or
  from an address that isn't private, gets a 404 here too;
- ADMIN_PASSWORD (HTTP Basic). With it unset there are no admin pages at all.
Form posts must come from these pages (Origin check), so another site open in
the same browser can't submit them.
"""
from __future__ import annotations

import asyncio
import base64
import csv
import hmac
import html
import io
import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .config import Settings
from .csms import CSMS, _is_private
from .db import DB
from .payments import ACTIVE, PaymentError, Payments
from .tariff import TZ

log = logging.getLogger(__name__)

READY = "Preparing"


def add_admin(app: FastAPI, settings: Settings, db: DB, payments: Payments, csms: CSMS, page) -> None:

    def guard(request: Request) -> None:
        if not settings.admin_password:
            raise HTTPException(404)
        client = request.client.host if request.client else ""
        if "cf-connecting-ip" in request.headers or not _is_private(client):
            raise HTTPException(404)
        if not _basic_ok(request.headers.get("authorization", ""), settings.admin_password):
            raise HTTPException(401, headers={"WWW-Authenticate": 'Basic realm="admin"'})
        if request.method == "POST":
            origin = request.headers.get("origin") or request.headers.get("referer") or ""
            if not origin or urlparse(origin).netloc != request.headers.get("host"):
                raise HTTPException(403, "Form posts must come from the admin page")

    def api_guard(request: Request) -> None:
        if not settings.api_token:
            raise HTTPException(404)
        client = request.client.host if request.client else ""
        if "cf-connecting-ip" in request.headers or not _is_private(client):
            raise HTTPException(404)
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), settings.api_token.encode()):
            raise HTTPException(401, headers={"WWW-Authenticate": "Bearer"})

    @app.post("/api/solar", dependencies=[Depends(api_guard)])
    async def api_solar(request: Request):
        """Home Assistant says whether there's spare solar: {"on": true|false}.
        While on, energy is billed at SOLAR_PRICE_PER_KWH (if set)."""
        try:
            on = (await request.json())["on"]
        except (ValueError, KeyError, TypeError):
            raise HTTPException(400, 'Send {"on": true} or {"on": false}') from None
        if not isinstance(on, bool):
            raise HTTPException(400, '"on" must be true or false')
        changed = db.set_solar(on)
        if changed:
            log.info("solar rate %s (from Home Assistant)", "on" if on else "off")
        return {"solar_rate_on": db.solar_active(), "changed": changed}

    @app.get("/api/status", dependencies=[Depends(api_guard)])
    def api_status():
        """Everything Home Assistant needs, in one read."""
        now = datetime.now(ZoneInfo(TZ))
        sockets = {}
        for c in settings.connectors:
            cp, _ = settings.sockets[c]
            live = [s for s in db.sessions_with_status(*ACTIVE, connector=c)]
            session, power_kw = None, 0.0
            if live:
                s = live[0]
                t = db.transaction_for_session(s["id"])
                readings = db.readings(t["id"]) if t else []
                energy = (readings[-1][1] - readings[0][1]) if readings else 0
                cost = payments.running_cost(s["id"], readings)[0] if len(readings) > 1 else Decimal(0)
                if len(readings) >= 2:
                    (t0, w0), (t1, w1) = readings[-2], readings[-1]
                    secs = (t1 - t0).total_seconds()
                    power_kw = round((w1 - w0) / secs * 3.6, 2) if secs > 0 else 0.0
                session = {
                    "id": s["id"], "status": s["status"].lower(),
                    "kind": "free" if s["payment_method"] == "manual" else "paid",
                    "energy_kwh": round(energy / 1000, 3), "cost": float(cost),
                    "hold": float(s["hold_amount"]),
                    "limit_kwh": s["limit_wh"] / 1000 if s["limit_wh"] else None,
                    "started_at": s["started_at"] or s["authorized_at"], "note": s["note"],
                }
            sockets[c] = {"state": csms.socket_state(c), "online": cp in csms.points, "charge_point": cp,
                          "power_kw": power_kw, "session": session}
        month = db.charges(now.strftime("%Y-%m"))
        today = [s for s in month if _local(s["authorized_at"])[:10] == now.strftime("%Y-%m-%d")]
        t_month, t_today = _totals(month), _totals(today)
        return {
            "currency": settings.currency,
            "sockets": sockets,
            "chargers": {cp: {"online": cp in csms.points, **csms.info.get(cp, {})} for cp in sorted(settings.charge_points)},
            "solar_rate_on": db.solar_active(),
            "today": {"kwh": float(t_today["paid_kwh"] + t_today["free_kwh"]), "taken": float(t_today["paid_gross"]),
                      "charges": t_today["paid_n"] + t_today["free_n"]},
            "month": {"kwh": float(t_month["paid_kwh"] + t_month["free_kwh"]), "taken": float(t_month["paid_gross"]),
                      "paypal_fees": float(t_month["paid_fee"]), "free_kwh": float(t_month["free_kwh"]),
                      "charges": t_month["paid_n"] + t_month["free_n"]},
        }

    def admin_page(title: str, body: str, refresh: int | None = None) -> str:
        return page(title, '<p class=back><a href="/admin">Admin</a> · <a href="/">Public page</a></p>' + body,
                    refresh=refresh, note_at_foot=False)

    @app.get("/admin", response_class=HTMLResponse, dependencies=[Depends(guard)])
    def overview(msg: str = ""):
        sockets = "".join(
            f'<tr><td><a href="/admin/chargers/{html.escape(settings.sockets[c][0])}">{html.escape(c)}</a></td>'
            f"<td>{html.escape(csms.socket_state(c))}</td>"
            f"<td class=small>{html.escape(settings.power_label(c))}</td></tr>"
            for c in settings.connectors
        )
        active = db.sessions_with_status(*ACTIVE)
        options = "".join(f'<option value="{html.escape(c)}">{html.escape(c)}</option>' for c in settings.connectors)
        body = (f'<p class=flash>{html.escape(msg)}</p>' if msg else "") + f"""
<h2>Sockets</h2><table class=sockets>{sockets}</table>
<h2>Authorise a charge</h2>
<p class=small>No payment is taken. The car must be plugged in; the charger starts at once and
stops at the limit. The session is priced at the tariff for the records.</p>
<form method="post" action="/admin/authorise" class=admin>
 <label>Socket <select name="socket">{options}</select></label>
 <label>Limit <input name="amount" inputmode="decimal" required size=6>
  <select name="unit"><option value="kwh">kWh</option><option value="gbp">£</option></select></label>
 <label>For / why <input name="note" maxlength=200 size=28 placeholder="e.g. minister's car"></label>
 <button type="submit">Authorise and start</button>
</form>
<h2>Charging now</h2>{_sessions_table(db, active, stop=True) if active else "<p class=small>Nothing.</p>"}
<h2>Recent</h2>{_sessions_table(db, db.charges(limit=10))}
<p><a href="/admin/charges">All charges, totals and CSV →</a></p>"""
        return admin_page("Admin", body, refresh=30)

    @app.get("/admin/charges", response_class=HTMLResponse, dependencies=[Depends(guard)])
    def charges(month: str = ""):
        month = month if MONTH.fullmatch(month) else ""
        rows = db.charges(month or None)
        months = " · ".join(
            (f"<strong>{_month_name(m)}</strong>" if m == month else f'<a href="/admin/charges?month={m}">{_month_name(m)}</a>')
            for m in db.charge_months())
        everything = "<strong>All</strong>" if not month else '<a href="/admin/charges">All</a>'
        t = _totals(rows)
        csv_link = f'/admin/charges.csv{"?month=" + month if month else ""}'
        body = f"""<p class=small>{everything}{" · " + months if months else ""}</p>
<table class=sessions>
<tr><td>Paid charges</td><td>{t["paid_n"]}</td><td>{t["paid_kwh"]:.2f} kWh</td><td>£{t["paid_gross"]:.2f} taken, £{t["paid_fee"]:.2f} PayPal fees, £{t["paid_gross"] - t["paid_fee"]:.2f} net</td></tr>
<tr><td>Free charges</td><td>{t["free_n"]}</td><td>{t["free_kwh"]:.2f} kWh</td><td>worth £{t["free_value"]:.2f} at the tariff</td></tr>
<tr><td>Other</td><td>{t["other_n"]}</td><td colspan=2 class=small>holds released, failed, or still running</td></tr>
</table>
<p><a href="{csv_link}">Download CSV</a></p>
<h2>{_month_name(month) if month else "All charges"}</h2>{_sessions_table(db, rows) if rows else "<p class=small>None.</p>"}"""
        return admin_page("Charges", body)

    @app.get("/admin/charges.csv", dependencies=[Depends(guard)])
    def charges_csv(month: str = ""):
        month = month if MONTH.fullmatch(month) else ""
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["date", "time", "socket", "status", "payment", "kwh", "charged", "paypal_fee", "net",
                    "value_at_tariff", "session_fee", "paypal_capture_id", "invoice_id", "note", "session"])
        for s in reversed(db.charges(month or None)):
            local = _local(s["authorized_at"])
            parts = json.loads(s["breakdown"]) if s["breakdown"] else {}
            value = _energy_value(s) if parts else None
            charged = Decimal(s["amount_charged"]) if s["amount_charged"] else None
            fee = Decimal(s["paypal_fee"]) if s["paypal_fee"] else None
            w.writerow([
                local[:10], local[11:16], s["connector"], s["status"].lower(), s["payment_method"] or "paypal",
                f"{s['energy_wh'] / 1000:.3f}" if s["energy_wh"] is not None else "",
                f"{charged:.2f}" if charged is not None else "", f"{fee:.2f}" if fee is not None else "",
                f"{charged - fee:.2f}" if charged is not None and fee is not None else "",
                f"{value:.2f}" if value is not None else "",
                parts.get("fee", {}).get("cost", ""), s["paypal_capture_id"] or "",
                f"{settings.invoice_prefix}{s['id']}" if s["paypal_capture_id"] else "", s["note"] or "", s["id"],
            ])
        name = f"ev-charges-{month or 'all'}.csv"
        return Response(out.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/admin/chargers/{cp}", response_class=HTMLResponse, dependencies=[Depends(guard)])
    def charger(cp: str, msg: str = ""):
        if cp not in settings.charge_points:
            raise HTTPException(404)
        info = csms.info.get(cp, {})
        online = cp in csms.points
        facts = [("Connection", "online" if online else "offline"), ("Protocol", info.get("protocol") or "?"),
                 ("Last heard from", info.get("last_seen") or "never"),
                 ("Make", info.get("vendor") or "?"), ("Model", info.get("model") or "?"),
                 ("Firmware", info.get("firmware") or "?"), ("Serial", info.get("serial") or "?")]
        for socket, (p, conn) in settings.sockets.items():
            if p == cp:
                facts.append((f"Connector {conn} ({socket})", csms.status.get((cp, conn), "unknown")))
        shown = csms.display.get(cp)
        last = csms.last_display.get(cp)
        facts.append(("Prices on its screen", ("yes" if shown else "no") + (
            f" (last: {last['message']} → {last['status']} at {last['at']})" if last else "")))
        table = "".join(f"<tr><th>{html.escape(k)}</th><td>{html.escape(str(v))}</td></tr>" for k, v in facts)
        cfg = csms.config.get(cp)
        if cfg:
            rows = "".join(
                f"<tr><td>{html.escape(k['key'])}</td><td>{html.escape(str(k.get('value', '')))}</td>"
                f"<td class=small>{'read-only' if k.get('readonly') else ''}</td></tr>" for k in cfg["keys"])
            settings_html = f"<p class=small>Read {html.escape(cfg['at'])}, {len(cfg['keys'])} settings.</p><table class=sessions>{rows}</table>"
        else:
            settings_html = "<p class=small>Not read yet.</p>"
        button = (f'<form method="post" action="/admin/chargers/{html.escape(cp)}/read"><button>Read settings from the charger</button></form>'
                  if online else "")
        body = (f'<p class=flash>{html.escape(msg)}</p>' if msg else "") + \
            f"<table>{table}</table><h2>Charger settings</h2>{button}{settings_html}"
        return admin_page(f"Charger {cp}", body)

    @app.post("/admin/chargers/{cp}/read", dependencies=[Depends(guard)])
    async def charger_read(cp: str):
        if cp not in settings.charge_points:
            raise HTTPException(404)
        try:
            keys = await csms.read_configuration(cp)
            msg = f"Read {len(keys)} settings."
        except Exception as e:
            msg = f"Couldn't read the settings: {e}"
        return RedirectResponse(f"/admin/chargers/{cp}?msg={quote(msg)}", status_code=303)

    @app.post("/admin/authorise", dependencies=[Depends(guard)])
    async def authorise(socket: str = Form(...), amount: str = Form(...), unit: str = Form(...), note: str = Form("")):
        try:
            value = Decimal(amount.strip().lstrip("£"))
        except InvalidOperation:
            return _back("That amount isn't a number.")
        if csms.socket_state(socket) != READY:
            return _back(f"{socket} isn't ready: plug the car in first (it shows {csms.socket_state(socket)}).")
        try:
            s = await asyncio.to_thread(
                payments.authorise_manual, socket, note=note.strip(),
                **({"limit_kwh": value} if unit == "kwh" else {"limit_gbp": value}))
        except PaymentError as e:
            return _back(str(e))
        if not await csms.remote_start(s):
            await asyncio.to_thread(payments.cancel, s["id"], "charger did not accept the start")
            return _back(f"The charger didn't start on {socket}; nothing was authorised.")
        return _back(f"Started {socket}: {value} {'kWh' if unit == 'kwh' else '£'}.")

    @app.post("/admin/sessions/{sid}/stop", dependencies=[Depends(guard)])
    async def stop(sid: str):
        s = db.session(sid)
        if not s:
            raise HTTPException(404)
        if s["status"] == "CHARGING":
            ok = await csms.remote_stop(sid, "stopped from admin")
            return _back("Stop sent." if ok else "The charger didn't accept the stop.")
        if s["status"] == "AUTHORISED":
            await asyncio.to_thread(payments.cancel, sid, "cancelled from admin")
            return _back("Cancelled; any hold released.")
        return _back("That session isn't running.")


MONTH = re.compile(r"\d{4}-\d{2}")
PAID = ("CAPTURED", "CAPTURE_PENDING")


def _energy_value(s: dict) -> Decimal:
    """What the energy was worth at the tariff, without the session fee (that covers PayPal)."""
    parts = json.loads(s["breakdown"]) if s["breakdown"] else {}
    return sum((Decimal(p["cost"]) for k, p in parts.items() if k != "fee"), Decimal(0))


def _local(ts: str) -> str:
    """UTC ISO -> local 'YYYY-MM-DD HH:MM'."""
    return datetime.fromisoformat(ts).astimezone(ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M")


def _month_name(m: str) -> str:
    return datetime.strptime(m, "%Y-%m").strftime("%b %Y")


def _totals(rows: list[dict]) -> dict:
    t = dict(paid_n=0, paid_kwh=Decimal(0), paid_gross=Decimal(0), paid_fee=Decimal(0),
             free_n=0, free_kwh=Decimal(0), free_value=Decimal(0), other_n=0)
    for s in rows:
        kwh = Decimal(s["energy_wh"] or 0) / 1000
        if s["status"] in PAID:
            t["paid_n"] += 1
            t["paid_kwh"] += kwh
            t["paid_gross"] += Decimal(s["amount_charged"] or 0)
            t["paid_fee"] += Decimal(s["paypal_fee"] or 0)
        elif s["payment_method"] == "manual" and s["status"] == "COMPLETED":
            t["free_n"] += 1
            t["free_kwh"] += kwh
            t["free_value"] += _energy_value(s) if s["breakdown"] else 0
        else:
            t["other_n"] += 1
    return t


def _back(msg: str) -> RedirectResponse:
    from urllib.parse import quote
    return RedirectResponse(f"/admin?msg={quote(msg)}", status_code=303)


def _sessions_table(db: DB, sessions: list[dict], stop: bool = False) -> str:
    rows = []
    for s in sessions:
        value = _energy_value(s) if s["breakdown"] else None
        if s["payment_method"] == "manual":
            limit = f"{s['limit_wh'] / 1000:g} kWh" if s["limit_wh"] else f"£{s['hold_amount']}"
            money = f"free ({limit} limit)" + (f", worth £{value:.2f}" if value is not None else "")
        else:
            money = f"£{s['amount_charged']}" if s["amount_charged"] else f"held £{s['hold_amount']}"
            if s["paypal_fee"]:
                money += f" (fee £{s['paypal_fee']})"
        energy = f"{s['energy_wh'] / 1000:.2f} kWh" if s["energy_wh"] is not None else ""
        button = (f'<form method="post" action="/admin/sessions/{s["id"]}/stop" style="margin:0">'
                  '<button class=small>Stop</button></form>') if stop else ""
        rows.append(
            f"<tr><td class=small>{html.escape(_local(s['authorized_at'] or s['created_at'])[5:])}</td>"
            f"<td>{html.escape(s['connector'])}</td><td>{html.escape(s['status'].lower())}</td>"
            f"<td>{energy}</td><td>{html.escape(money)}</td><td class=small>{html.escape(s['note'] or '')}</td>"
            f"<td>{button}</td></tr>")
    return f"<table class=sessions>{''.join(rows)}</table>"


def _basic_ok(header: str, password: str) -> bool:
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        _, _, got = base64.b64decode(value, validate=True).decode().partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return hmac.compare_digest(got.encode(), password.encode())
