"""The receipt a driver gets when their charge is paid for.

Most drivers here pay by card as a guest, so there is no PayPal account for
them to look in. PayPal emails them once, at authorisation, showing the hold -
and never again unless the final amount is *higher*, which it never is. So
without this they see "£20.00 held" and nothing else, and their only record of
what they actually paid is a card statement days later.

The address comes from the PayPal order and is kept only until the charge is
settled, then cleared. Nothing is sent unless SMTP is configured, so a site that
would rather hold no addresses at all simply leaves it unset.

Sending happens on a background thread: a slow mail server must never hold up
the charger or the settlement, and a failed send must never fail a charge. The
money is already taken and recorded; an undelivered receipt is a nuisance, not
an error worth unwinding anything for.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
import threading
from decimal import Decimal
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

log = logging.getLogger(__name__)

TIMEOUT = 30.0


def _money(currency: str, value: Decimal) -> str:
    return f"£{value:.2f}" if currency == "GBP" else f"{value:.2f} {currency}"


def charged_body(settings, session: dict, parts: list[str]) -> tuple[str, str]:
    """Subject and body for a charge that was paid for."""
    site = settings.brand_name or "the charger"
    cost = Decimal(session["amount_charged"] or 0)
    kwh = (session["energy_wh"] or 0) / 1000
    lines = [
        f"Thank you for charging at {site}.",
        "",
        f"  Socket    {session['connector']}",
        f"  Energy    {kwh:.2f} kWh",
        f"  Paid      {_money(settings.currency, cost)}",
    ]
    lines += [f"            {p}" for p in parts]
    if session.get("hold_amount"):
        held = _money(settings.currency, Decimal(session["hold_amount"]))
        lines.append(f"  Held      {held} - the rest has been released")
    lines += ["", "This is the final amount. Nothing further will be taken."]
    if settings.support_contact:
        lines += ["", settings.support_contact]
    return f"Your charge at {site}: {_money(settings.currency, cost)}", "\n".join(lines) + "\n"


def voided_body(settings, session: dict, reason: str) -> tuple[str, str]:
    """Subject and body when no energy was taken and nothing is charged."""
    site = settings.brand_name or "the charger"
    lines = [f"You have not been charged at {site}.", ""]
    if session.get("hold_amount"):
        held = _money(settings.currency, Decimal(session["hold_amount"]))
        lines.append(f"The {held} hold has been released. It may take a few days")
        lines.append("to disappear from your statement, depending on your bank.")
    else:
        lines.append("The hold has been released.")
    lines += ["", f"Socket {session['connector']}. Reason: {reason}."]
    if settings.support_contact:
        lines += ["", settings.support_contact]
    return f"No charge taken at {site}", "\n".join(lines) + "\n"


def send(settings, to: str, subject: str, body: str) -> bool:
    """Send one message. Returns whether it went. Never raises."""
    if not settings.receipts_enabled or not to:
        return False
    msg = EmailMessage()
    sender = f"{settings.brand_name} <{settings.mail_from}>" if settings.brand_name else settings.mail_from
    msg["From"] = sender
    msg["To"] = to
    msg["Reply-To"] = settings.mail_reply_to or settings.mail_from
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=settings.mail_from.partition("@")[2] or None)
    msg["Auto-Submitted"] = "auto-generated"      # so holiday autoresponders stay quiet
    msg.set_content(body)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=TIMEOUT) as s:
            s.ehlo()
            s.starttls(context=ssl.create_default_context())
            s.ehlo()
            s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
    except Exception:
        # Deliberately swallowed: the charge is already settled and recorded.
        log.warning("receipt to the driver could not be sent", exc_info=True)
        return False
    log.info("receipt sent")       # not the address: it is about to be forgotten
    return True


def send_in_background(settings, to: str, subject: str, body: str) -> threading.Thread | None:
    if not settings.receipts_enabled or not to:
        return None
    t = threading.Thread(target=send, args=(settings, to, subject, body),
                         name="receipt", daemon=True)
    t.start()
    return t
