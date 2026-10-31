"""Public web side: pay pages, PayPal button endpoints, PayPal webhook.

Only /, /pay/*, /static/* and /webhooks/paypal reach this through the tunnel
(deploy/cloudflared/config.yml.example), so nothing here may be admin-only.

Run: uvicorn --factory evcharge.web:create_app
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import html
import json
import logging
import re
from pathlib import Path
from urllib.parse import quote
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel

from . import config
from .admin import add_admin
from .csms import CSMS, OFFLINE
from .db import DB
from .payments import PaymentError, Payments, session_tariff
from .paypal import PayPal, PayPalError
from .tariff import FEE, SOLAR, Tariff

log = logging.getLogger(__name__)

# A link to this phone's live session (remembered by the status page), on every other page.
YOUR_SESSION = """<p id=yours class=back hidden><a href="">Your charging session →</a></p><script>
try { const s = localStorage.getItem("evcharge.session");
  if (s && !location.pathname.endsWith(s)) { const p = document.getElementById("yours");
    p.firstChild.href = "/pay/session/" + encodeURIComponent(s); p.hidden = false; } } catch (e) {}
</script>"""
RATE_RULE = "<p>Each kWh is charged at the rate in force when it is delivered.</p>"
HOW_IT_WORKS = """<p>PayPal holds an amount while you charge. When you finish, you pay only for
the energy used and the rest is released. Charging stops if the cost reaches
the amount held.</p>"""
NO_DATA = "We keep no payment or personal data."
PROJECT_URL = "https://github.com/JR4AA/community-ev-charging"
POWERED_BY = (f'<p class="small powered">Powered by <a href="{PROJECT_URL}" rel="noopener">'
              'community-ev-charging</a>, open source.</p>')
READY = "Preparing"   # cable connected, no transaction yet
IN_USE = ("Charging", "SuspendedEV", "SuspendedEVSE", "Finishing", "Reserved")
ORDER_ID = re.compile(r"^[A-Z0-9]{10,40}$")
SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{16,40}$")

STATUS_TEXT = {
    "CREATED": "Waiting for PayPal approval.",
    "AUTHORISED": "Payment held. Starting the charger…",
    "CHARGING": "Charging.",
    "CAPTURE_PENDING": "Charging finished. PayPal is completing the payment.",
    "CAPTURED": "Charging finished and paid. Thank you!",
    "VOIDED": "Finished. You have not been charged, and the hold has been released.",
    "ABANDONED": "This payment was not completed. You have not been charged.",
    "FAILED": "Something went wrong with this payment.",
    "COMPLETED": "Charging finished. No payment was needed.",
    "CANCELLED": "Cancelled.",
}


LABELS = {"offpeak": "Off-peak", "peak": "Peak", "standard": "Price", SOLAR: "Solar", FEE: "Charge fee"}


def _label(name: str) -> str:
    return LABELS.get(name, name.replace("_", " ").replace("-", " ").capitalize())


def _pence(price: Decimal) -> str:
    return f"{(price * 100).normalize():f}p"


def _rates_table(tariff: Tariff, solar_on: bool, now_band: str | None) -> str:
    rows = []
    rates = sorted(tariff.rates(), key=lambda r: r[1], reverse=True)   # dearest first: peak, then off-peak
    # The band covering most of the day is "the rest of the time": no hours listed.
    usual = max(tariff.rates(), key=lambda r: sum(b.end - b.start for b in tariff.bands if b.name == r[0]))[0]
    for name, price, spans in rates:
        when = "" if name == usual else ", ".join(s.replace("-", "–") for s in spans)
        mark = " <strong>← now</strong>" if name == now_band and not solar_on else ""
        rows.append(f"<tr><th>{html.escape(_label(name))}</th><td>{_pence(price)} per kWh</td>"
                    f"<td class=small>{html.escape(when)}{mark}</td></tr>")
    if tariff.solar_price is not None:
        mark = " <strong>← now</strong>" if solar_on else ""
        rows.append(f"<tr><th>Solar</th><td>{_pence(tariff.solar_price)} per kWh</td>"
                    f"<td class=small>when there is spare solar power{mark}</td></tr>")
    if tariff.session_fee:
        rows.append(f"<tr><th>Plus</th><td>{_pence(tariff.session_fee)} per charge</td>"
                    "<td class=small>(the PayPal fee)</td></tr>")
    return f"<table class=rates>{''.join(rows)}</table>"


class OrderRequest(BaseModel):
    connector: str
    hold: Decimal | None = None


def create_app(settings: config.Settings | None = None, paypal: PayPal | None = None) -> FastAPI:
    settings = settings or config.from_env()
    db = DB(settings.db_path)
    payments = Payments(settings, db, paypal or PayPal(settings))
    csms = CSMS(settings, db, payments)
    remote_started: set[str] = set()
    page = functools.partial(_page, settings=settings)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.ocpp_server = await csms.serve()
        task = asyncio.create_task(_housekeeping(payments, csms)) if settings.paypal_configured else None
        yield
        if task:
            task.cancel()
        app.state.ocpp_server.close()
        await app.state.ocpp_server.wait_closed()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.payments = payments
    app.state.csms = csms
    add_admin(app, settings, db, payments, csms, page)

    @app.exception_handler(PaymentError)
    async def payment_error(_: Request, e: PaymentError):
        return JSONResponse({"detail": str(e), "paypal": e.paypal_name}, status_code=e.code)

    @app.get("/static/brand/{name}")
    def brand_file(name: str):
        # Only the configured logo, never an arbitrary file from the directory.
        path = Path(settings.brand_dir) / settings.brand_logo
        if not settings.brand_logo or name != settings.brand_logo or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/pay/health", response_class=PlainTextResponse)
    def health():
        return "ok\n"

    @app.get("/", response_class=HTMLResponse)
    def index():
        rows = []
        for c in settings.connectors:
            state = csms.socket_state(c)
            if payments.connector_busy(c) or state in IN_USE:
                what = "in use"
            elif state == OFFLINE:
                what = "offline"
            elif state in ("Faulted", "Unavailable"):
                what = "out of service"
            elif state == READY:
                what = "car plugged in"
            else:
                what = "free"
            power = settings.power_label(c)
            rows.append(f'<tr><td><a href="/pay/{html.escape(c)}">Socket {html.escape(c)}</a></td>'
                        f'<td class=small>{html.escape(power)}</td><td>{what}</td></tr>')
        body = (f"{_note(settings.page_note)}<h2>Sockets</h2><table class=sockets>{''.join(rows)}</table>"
                "<p class=small>Plug in, then open the socket's page (or scan its QR code) to pay.</p>"
                f"<h2>Prices</h2>{RATE_RULE}{_rates_table(settings.tariff, db.solar_active(), _now_band(settings))}{HOW_IT_WORKS}"
                f"<p>You can hold {', '.join(f'£{h:.0f}' for h in settings.hold_options)}. "
                f"Pay by {'debit or credit card' if settings.payment_methods == 'card' else 'PayPal or card'}. {NO_DATA}</p>")
        return page("EV charging", body, refresh=30, note_at_foot=False)

    @app.get("/pay/{connector}", response_class=HTMLResponse)
    def pay_page(connector: str):
        if connector not in settings.connectors:
            raise HTTPException(404)
        if not settings.paypal_configured:
            return page("EV charging", "<p>Payments are not set up yet.</p>")
        title = f"Socket {connector}"
        state = csms.socket_state(connector)
        if payments.connector_busy(connector) or state in IN_USE:
            return page(title, back=True, subtitle=settings.power_label(connector), body="<p>This socket is in use. Please try the other one.</p>", refresh=30)
        if state == OFFLINE:
            return page(title, back=True, subtitle=settings.power_label(connector), body="<p>This charger is offline at the moment. Please try again later.</p>", refresh=30)
        if state in ("Faulted", "Unavailable"):
            return page(title, back=True, subtitle=settings.power_label(connector), body="<p>This socket is out of service at the moment. Sorry.</p>", refresh=30)
        if state != READY:
            return page(title, back=True, subtitle=settings.power_label(connector), body="<p><strong>Plug your car in first.</strong> This page will then let you pay.</p>"
                         + RATE_RULE + _rates_table(settings.tariff, db.solar_active(), _now_band(settings)), refresh=5)
        return page(title, back=True, subtitle=settings.power_label(connector), body=_pay_body(settings, connector, db.solar_active()))

    @app.post("/pay/api/orders")
    def create_order(req: OrderRequest):
        _need_paypal(settings)
        if req.connector in settings.sockets and csms.socket_state(req.connector) != READY:
            raise PaymentError(409, "Please plug your car in first")
        _, order_id = payments.create_order(req.connector, req.hold)
        return {"orderID": order_id}

    @app.post("/pay/api/orders/{order_id}/authorize")
    async def authorize(order_id: str):
        _need_paypal(settings)
        if not ORDER_ID.match(order_id):
            raise HTTPException(404)
        s = await asyncio.to_thread(payments.authorize, order_id)
        if s["status"] == "AUTHORISED" and s["id"] not in remote_started:
            remote_started.add(s["id"])
            if not await csms.remote_start(s):
                await asyncio.to_thread(payments.cancel, s["id"], "charger did not accept the start")
                raise PaymentError(503, "The charger did not start, so the payment has been cancelled. "
                                        "You have not been charged.")
        return {"session": s["id"], "status": s["status"]}

    @app.post("/pay/session/{sid}/stop")
    async def stop(sid: str):
        s = db.session(sid) if SESSION_ID.match(sid) else None
        if not s:
            raise HTTPException(404)
        if s["status"] == "CHARGING":
            await csms.remote_stop(sid, "driver pressed Stop")
        return RedirectResponse(f"/pay/session/{sid}", status_code=303)

    @app.get("/pay/session/{sid}", response_class=HTMLResponse)
    def session_page(sid: str):
        s = db.session(sid) if SESSION_ID.match(sid) else None
        if not s:
            raise HTTPException(404)
        rows = [("Socket", s["connector"])]
        for name, part in (json.loads(s["breakdown"]) if s["breakdown"] else {}).items():
            if name == FEE:
                rows.append((_label(name), f"£{part['cost']}"))
            else:
                rows.append((_label(name), f"{part['wh'] / 1000:.2f} kWh at {_pence(Decimal(part['price']))} = £{part['cost']}"))
        if s["energy_wh"] is not None:
            rows.append(("Energy", f"{s['energy_wh'] / 1000:.2f} kWh"))
        tx = db.transaction_for_session(sid) if s["status"] == "CHARGING" else None
        if tx:
            readings = db.readings(tx["id"])
            so_far, _ = payments.running_cost(sid, readings)
            rows.append(("So far", f"{(readings[-1][1] - readings[0][1]) / 1000:.2f} kWh, £{so_far}"))
        if s["amount_charged"] is not None:
            rows.append(("Charged", f"£{s['amount_charged']}"))
        elif s["status"] in ("AUTHORISED", "CHARGING"):
            rows.append(("Held", f"£{s['hold_amount']} (you pay only for what you use)"))
        table = "".join(f"<tr><th>{html.escape(k)}</th><td>{html.escape(v)}</td></tr>" for k, v in rows)
        live = s["status"] in ("CREATED", "AUTHORISED", "CHARGING", "CAPTURE_PENDING")
        body = f"<p><strong>{html.escape(STATUS_TEXT.get(s['status'], s['status']))}</strong></p><table>{table}</table>"
        if s["status"] == "FAILED":
            body += f"<p>{html.escape(settings.support_contact or 'Please contact the owner of this charger.')}</p>"
        if not s["breakdown"]:
            body += "<h2>Rates</h2>" + _rates_table(session_tariff(s), False, None)
        if tx:
            body += (f'<form method="post" action="/pay/session/{sid}/stop">'
                     '<button class=stop type="submit">Stop charging</button></form>')
        if live:
            body += "<p class=small>Keep this page to check progress. It refreshes itself.</p>"
        # Remember the session on this phone while it's live, so the other pages can link back to it.
        body += (f"<script>try {{ {'localStorage.setItem' if live else 'localStorage.removeItem'}"
                 f"(\"evcharge.session\"{', ' + json.dumps(sid) if live else ''}); }} catch (e) {{}}</script>")
        return page(f"Socket {s['connector']}", body, refresh=15 if live else None,
                    subtitle=settings.power_label(s["connector"]))

    @app.post("/webhooks/paypal")
    async def paypal_webhook(request: Request):
        if not settings.paypal_webhook_id:
            raise HTTPException(503, "webhook not configured")
        raw = await request.body()
        try:
            event = json.loads(raw)
        except ValueError:
            raise HTTPException(400) from None
        try:
            ok = await asyncio.to_thread(payments.paypal.verify_webhook, dict(request.headers), event)
        except PayPalError:
            log.exception("webhook verification call failed")
            raise HTTPException(503) from None  # PayPal retries
        if not ok:
            log.warning("webhook %s failed verification", event.get("id"))
            raise HTTPException(400)
        note = await asyncio.to_thread(payments.handle_webhook, event)
        log.info("webhook %s %s: %s", event.get("id"), event.get("event_type"), note)
        return {"ok": True}

    return app


async def _housekeeping(payments: Payments, csms: CSMS) -> None:
    while True:
        for job in (payments.expire_unused, csms.retry_settlements):
            try:
                await asyncio.to_thread(job)
            except Exception:
                log.exception("%s failed", job.__name__)
        await asyncio.sleep(60)


def _now_band(settings: config.Settings) -> str:
    return settings.tariff.band_at(datetime.now(timezone.utc)).name


def _need_paypal(settings: config.Settings) -> None:
    if not settings.paypal_configured:
        raise HTTPException(503, "Payments are not set up yet")


def _pay_body(settings: config.Settings, connector: str, solar_on: bool) -> str:
    sdk = (
        "https://www.paypal.com/sdk/js?client-id=" + html.escape(settings.paypal_client_id)
        + f"&currency={settings.currency}&intent=authorize&components=buttons&disable-funding=paylater,credit"
    )
    tariff = settings.tariff
    now_band = _now_band(settings)
    card_only = settings.payment_methods == "card"
    # One row per hold: how many kWh it covers at each rate, dearest rate first.
    rates = sorted(((n, p) for n, p, _ in tariff.rates()), key=lambda r: r[1], reverse=True)
    head = "".join(f"<th>{html.escape(_label(n))}</th>" for n, _ in rates)
    choices = "".join(
        f'<tr class=hold><td><label><input type="radio" name="hold" value="{h:.2f}"{" checked" if h == settings.hold_default else ""}> '
        f'£{h:.0f}</label></td>' + "".join(f"<td>{settings.kwh_for(h, p)} kWh</td>" for _, p in rates) + "</tr>"
        for h in settings.hold_options
    )
    return f"""
{RATE_RULE}
{_rates_table(tariff, solar_on, now_band)}
{HOW_IT_WORKS}
<fieldset aria-label="Amount to hold">
<table class=holds>
<tr class=top><th>Amount to hold</th><th colspan={len(rates)}>Estimated max charge</th></tr>
<tr><th></th>{head}</tr>{choices}</table>
<p class=small>The energy each amount covers, if it is all at that rate.</p></fieldset>
<p class=small>{"Pay by debit or credit card." if card_only else
"Pay with PayPal or a debit or credit card. You do not need a PayPal account to pay by card."} {NO_DATA}</p>
<div id="buttons"></div>
<p id="msg" role="alert"></p>
<script src="{sdk}"></script>
<script>
const connector = {json.dumps(connector)};
const msg = (t) => document.getElementById("msg").textContent = t;
// Tapping anywhere on a hold row selects it.
document.querySelectorAll("tr.hold").forEach(tr => tr.addEventListener("click", () => tr.querySelector("input").checked = true));
async function post(url, body) {{
  const r = await fetch(url, {{method: "POST", headers: {{"Content-Type": "application/json"}},
                              body: JSON.stringify(body || {{}})}});
  const d = await r.json().catch(() => ({{}}));
  if (!r.ok) {{ const e = new Error(d.detail || "Something went wrong"); e.paypal = d.paypal; throw e; }}
  return d;
}}
const handlers = {{
  createOrder: () => post("/pay/api/orders", {{connector,
      hold: document.querySelector("input[name=hold]:checked").value}}).then(d => d.orderID).catch(e => {{ msg(e.message); throw e; }}),
  onApprove: (data, actions) => post(`/pay/api/orders/${{data.orderID}}/authorize`)
    .then(d => {{ location.href = `/pay/session/${{d.session}}`; }})
    .catch(e => {{ if (e.paypal === "INSTRUMENT_DECLINED") return actions.restart(); msg(e.message); }}),
  onCancel: () => msg("Payment cancelled. You have not been charged."),
  onError: () => msg("PayPal could not start the payment. Please try again."),
}};
// Card only: just PayPal's guest card checkout (cheapest fee). If PayPal says
// that isn't available here, fall back to the normal buttons so the driver can still pay.
const card = {"true" if card_only else "false"} && paypal.Buttons({{...handlers, fundingSource: paypal.FUNDING.CARD}});
if (card && card.isEligible()) card.render("#buttons");
else paypal.Buttons(handlers).render("#buttons");
</script>"""


def _page(title: str, body: str, settings: config.Settings, refresh: int | None = None, subtitle: str = "",
          back: bool = False, note_at_foot: bool = True) -> str:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    logo = f"/static/brand/{quote(settings.brand_logo)}" if settings.brand_logo else ""
    icon = f'<link rel="icon" href="{logo}">' if logo else ""
    header = ""
    if settings.brand_name or logo:
        img = f'<img src="{logo}" alt="">' if logo else ""
        header = f'<header class=brand>{img}<span>{html.escape(settings.brand_name)}</span></header>'
    footer = f"<p class=small>{html.escape(settings.footer_text)}</p>" if settings.footer_text else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{meta}{icon}
<title>{html.escape(title)}</title>
<style>
 :root {{ --accent: {settings.brand_color}; --accent-ui: var(--accent); accent-color: var(--accent-ui); }}
 header.brand {{ display: flex; align-items: center; gap: .9rem; padding-bottom: .8rem; margin-bottom: .4rem;
                 border-bottom: 4px solid var(--accent); }}
 header.brand img {{ height: 56px; width: auto; border-radius: 6px; }}
 header.brand span {{ text-transform: uppercase; letter-spacing: .25em; font-size: .95rem; line-height: 1.4; }}
 table.holds tr.hold:has(input:checked) td:first-child {{ box-shadow: inset 4px 0 0 var(--accent-ui); }}
 body {{ font: 17px/1.5 system-ui, sans-serif; max-width: 30rem; margin: 0 auto; padding: 1rem; color: #1a1a1a; background: #fff; }}
 h1 {{ font-size: 1.4rem; }} h2 {{ font-size: 1.1rem; }} td {{ padding-right: .8rem; }} .small {{ font-size: .9rem; color: #555; }}
 fieldset {{ border: 0; padding: 0; margin: 0 0 1rem; }} legend {{ font-weight: 600; }}
 label {{ display: block; padding: .4rem 0; }} table.holds td, table.holds th {{ padding-right: 1.4rem; }} table.holds th {{ font-weight: 400; font-size: .9rem; opacity: .75; text-align: left; }} table.holds tr.top th {{ font-weight: 600; font-size: 1rem; opacity: 1; padding-bottom: .2rem; }}
 table.holds {{ border-collapse: collapse; width: 100%; }} table.holds td {{ padding-left: .4rem; }}
 table.holds tr.hold {{ border-top: 1px solid rgba(128,128,128,.35); cursor: pointer; }}
 table.holds tr.hold:last-child {{ border-bottom: 1px solid rgba(128,128,128,.35); }}
 table.holds tr.hold:has(input:checked) {{ background: rgba(128,128,128,.18); }} input[type=radio] {{ transform: scale(1.3); margin-right: .5rem; }}
 /* PayPal's buttons and card form assume a white page, so give them one in dark mode too. */
 #buttons {{ background: #fff; border-radius: 10px; padding: 14px 12px 8px; }}
 h1 .power {{ font-size: .95rem; font-weight: 400; opacity: .75; white-space: nowrap; }}
 .back {{ margin: .6rem 0 0; font-size: .9rem; }} .back a, table.sockets a {{ color: inherit; }}
 table.sockets td {{ padding: .35rem 1rem .35rem 0; }}
 .flash {{ padding: .5rem .8rem; border-left: 4px solid var(--accent-ui); background: rgba(128,128,128,.15); }}
 form.admin label {{ display: block; padding: .3rem 0; }} form.admin input, form.admin select, form.admin button {{ font: inherit; }}
 table.sessions {{ font-size: .9rem; border-collapse: collapse; }} table.sessions td {{ padding: .3rem .6rem .3rem 0; border-top: 1px solid rgba(128,128,128,.3); }}
 .powered {{ margin-top: 1.5rem; opacity: .8; }} .powered a {{ color: inherit; }}
 .note {{ font-size: .9rem; margin-top: 1.5rem; }}
 button.stop {{ font: inherit; font-weight: 600; padding: .7rem 1.4rem; border: 0; border-radius: 8px; background: #b00020; color: #fff; }}
 th {{ text-align: left; padding-right: 1rem; font-weight: 600; }} #msg {{ color: #a00; }}
 @media (prefers-color-scheme: dark) {{ :root {{ --accent-ui: color-mix(in srgb, var(--accent) 50%, white); }} body {{ color: #eee; background: #111; }} .small {{ color: #aaa; }} #msg {{ color: #f88; }} }}
</style></head>
<body>{header}{YOUR_SESSION}{'<p class=back><a href="/">← All sockets and prices</a></p>' if back else ""}<h1>{html.escape(title)}{f' <span class=power>{html.escape(subtitle)}</span>' if subtitle else ""}</h1>{body}
{_note(settings.page_note) if note_at_foot else ""}{footer}{POWERED_BY}</body></html>"""


def _note(text: str) -> str:
    """PAGE_NOTE: plain text, a blank line ("\\n\\n" in the env file) between paragraphs."""
    paras = [p.strip() for p in text.replace("\\n", "\n").split("\n\n") if p.strip()]
    return "".join(f"<p class=note>{html.escape(p)}</p>" for p in paras)
