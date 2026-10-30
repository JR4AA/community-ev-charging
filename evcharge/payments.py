"""Charging-session payments: the state machine between the pay page, PayPal and
(later) the OCPP side.

    CREATED ──authorize──► AUTHORISED ──charger starts──► CHARGING
       │                      │                              │
       └─► ABANDONED          ├─► VOIDED (unused, cancelled) ├─► CAPTURED  (cost > 0)
           (never paid)       │                              ├─► CAPTURE_PENDING ─► CAPTURED
                              └──────────── settle ──────────┴─► VOIDED    (no energy)
    FAILED: PayPal refused a step; `error` says which.
    Manual (admin) sessions start AUTHORISED with no PayPal, and end COMPLETED or CANCELLED.

The OCPP side (csms.py) calls mark_charging(), running_cost(), settle() and cancel().
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from .config import Settings
from .db import DB, now
from .paypal import PayPal, PayPalError
from .tariff import Reading, Tariff

log = logging.getLogger(__name__)

ACTIVE = ("AUTHORISED", "CHARGING")
# Orders the driver never approved; nothing is held, so this is bookkeeping only.
ABANDON_AFTER = timedelta(hours=3)


class PaymentError(Exception):
    """Something the driver should be told about. `code` maps to an HTTP status."""

    def __init__(self, code: int, message: str, paypal_name: str | None = None):
        self.code = code
        self.paypal_name = paypal_name
        super().__init__(message)


def session_tariff(s: dict) -> Tariff:
    return Tariff.from_json(s["tariff"]) if s.get("tariff") else Tariff.flat(Decimal(s["price_per_kwh"]))


def _fee(capture: dict) -> str | None:
    """PayPal's fee on a capture, when it reports one (completed captures)."""
    return ((capture.get("seller_receivable_breakdown") or {}).get("paypal_fee") or {}).get("value")


def _utc(t: datetime) -> str:
    """Same format as db.now(), so the strings compare in time order."""
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def _age(ts: str, at: datetime) -> timedelta:
    return at - datetime.fromisoformat(ts)


class Payments:
    def __init__(self, settings: Settings, db: DB, paypal: PayPal):
        self.settings = settings
        self.db = db
        self.paypal = paypal
        # One session changes state at a time; volumes are tiny and it keeps
        # "is this socket free?" checks honest.
        self._lock = threading.RLock()

    # -- driver side -------------------------------------------------------

    def connector_busy(self, connector: str) -> bool:
        return bool(self.db.sessions_with_status(*ACTIVE, connector=connector))

    def create_order(self, connector: str, hold: Decimal | None = None) -> tuple[str, str]:
        """Returns (session id, PayPal order id) for the buttons to approve."""
        if connector not in self.settings.connectors:
            raise PaymentError(404, "Unknown socket")
        hold = self.settings.hold_default if hold is None else hold
        if hold not in self.settings.hold_options:
            raise PaymentError(400, "That amount is not available")
        with self._lock:
            if self.connector_busy(connector):
                raise PaymentError(409, "This socket is already in use")
            sid = secrets.token_urlsafe(16)
            self.db.insert_session(
                id=sid,
                id_tag=sid[:20],          # OCPP idTag is at most 20 characters
                connector=connector,
                status="CREATED",
                hold_amount=f"{hold:.2f}",
                price_per_kwh=str(self.settings.tariff.band_at(datetime.now(timezone.utc)).price),
                tariff=self.settings.tariff.to_json(),
            )
            try:
                order = self.paypal.create_order(
                    amount=hold,
                    reference=sid,
                    description=self._description(connector, sid),
                    request_id=f"order-{sid}",
                )
            except PayPalError as e:
                self._fail(sid, f"create order: {e} {e.body}")
                raise PaymentError(502, "PayPal could not start the payment") from e
            self.db.update_session(sid, paypal_order_id=order["id"])
            self.db.log(sid, "order_created", order["id"])
            return sid, order["id"]

    def authorize(self, order_id: str) -> dict:
        """The driver approved in PayPal: place the hold. Safe to call twice."""
        with self._lock:
            s = self.db.session_by_order(order_id)
            if not s:
                raise PaymentError(404, "Unknown payment")
            if s["status"] != "CREATED":
                return s
            if self.connector_busy(s["connector"]):
                # Someone else paid for this socket first. No hold placed yet.
                self.db.update_session(s["id"], status="ABANDONED", error="socket taken before authorisation")
                self.db.log(s["id"], "abandoned", "socket busy at authorise")
                raise PaymentError(409, "Someone else has just started on this socket. You have not been charged.")
            try:
                auth = self.paypal.authorize_order(order_id, request_id=f"authorize-{order_id}")
            except PayPalError as e:
                name = e.body.get("details", [{}])[0].get("issue") if isinstance(e.body, dict) else None
                if name == "INSTRUMENT_DECLINED":
                    # The buttons restart with another funding source; the order stays usable.
                    self.db.log(s["id"], "declined", str(e.body))
                    raise PaymentError(402, "Payment method declined", paypal_name=name) from e
                self._fail(s["id"], f"authorize: {e} {e.body}")
                raise PaymentError(502, "PayPal did not accept the payment") from e
            if auth.get("status") != "CREATED":
                # PENDING = PayPal reviewing. Can't rely on it to cover a charge.
                self.db.update_session(
                    s["id"], status="FAILED", paypal_authorization_id=auth.get("id"),
                    error=f"authorization status {auth.get('status')}",
                )
                self.db.log(s["id"], "auth_not_usable", str(auth))
                raise PaymentError(402, "PayPal has not approved this payment yet. You have not been charged.")
            self.db.update_session(
                s["id"],
                status="AUTHORISED",
                paypal_authorization_id=auth["id"],
                authorization_expires=auth.get("expiration_time"),
                authorized_at=now(),
            )
            self.db.log(s["id"], "authorised", auth["id"])
            return self.db.session(s["id"])

    # -- admin side --------------------------------------------------------

    def authorise_manual(self, connector: str, *, limit_gbp: Decimal | None = None,
                         limit_kwh: Decimal | None = None, note: str = "") -> dict:
        """A charge the admin allows without payment, limited by money or energy.
        Priced at the tariff anyway, so the value shows in the records."""
        if connector not in self.settings.connectors:
            raise PaymentError(404, "Unknown socket")
        if (limit_gbp is None) == (limit_kwh is None):
            raise PaymentError(400, "Give a limit in £ or in kWh")
        limit = limit_gbp if limit_gbp is not None else limit_kwh
        if limit <= 0 or limit > 1000:
            raise PaymentError(400, "That limit isn't sensible")
        tariff = self.settings.tariff
        if limit_kwh is not None:
            hold = (limit_kwh * tariff.max_price + tariff.session_fee).quantize(Decimal("0.01"), ROUND_HALF_UP)
            limit_wh = int(limit_kwh * 1000)
        else:
            hold, limit_wh = limit_gbp.quantize(Decimal("0.01")), None
        with self._lock:
            if self.connector_busy(connector):
                raise PaymentError(409, "This socket is already in use")
            sid = secrets.token_urlsafe(16)
            self.db.insert_session(
                id=sid, id_tag=sid[:20], connector=connector, status="AUTHORISED",
                hold_amount=f"{hold:.2f}", price_per_kwh=str(tariff.band_at(datetime.now(timezone.utc)).price),
                tariff=tariff.to_json(), payment_method="manual", limit_wh=limit_wh, note=note[:200],
                authorized_at=now(),
            )
            self.db.log(sid, "manual_authorised", f"{'£' + str(hold) if limit_wh is None else f'{limit_kwh} kWh'}: {note}")
            return self.db.session(sid)

    # -- charger side ------------------------------------------------------

    def mark_charging(self, sid: str) -> None:
        with self._lock:
            s = self._get(sid)
            if s["status"] != "AUTHORISED":
                raise PaymentError(409, f"session is {s['status']}")
            self.db.update_session(sid, status="CHARGING", started_at=now())
            self.db.log(sid, "charging")

    def running_cost(self, sid: str, readings: list[Reading]) -> tuple[Decimal, dict]:
        """Cost so far and its breakdown, rounded and capped at the hold. The OCPP
        side stops the charger when this reaches the hold."""
        s = self._get(sid)
        solar = self.db.solar_periods(_utc(readings[0][0]), _utc(readings[-1][0])) if readings else []
        raw, breakdown = session_tariff(s).cost(readings, solar)
        return min(raw.quantize(Decimal("0.01"), ROUND_HALF_UP), Decimal(s["hold_amount"])), breakdown

    def settle(self, sid: str, energy_wh: int | None = None, readings: list[Reading] | None = None) -> dict:
        """Charge for the energy delivered and release the rest of the hold.

        `readings` are the charger's (time, meter Wh) samples, so each kWh is priced
        at the rate in force when it was delivered. Given only `energy_wh`, the
        energy is spread evenly from the start of the session until now."""
        with self._lock:
            s = self._get(sid)
            if s["status"] not in ACTIVE:
                if s["status"] in ("CAPTURED", "CAPTURE_PENDING", "VOIDED", "COMPLETED"):
                    return s  # already settled
                raise PaymentError(409, f"session is {s['status']}")
            if readings is None:
                start = datetime.fromisoformat(s["started_at"] or s["authorized_at"])
                readings = [(start, 0), (datetime.now(timezone.utc), int(energy_wh or 0))]
            energy_wh = max(readings[-1][1] - readings[0][1], 0)
            cost, breakdown = self.running_cost(sid, readings)
            self.db.update_session(sid, energy_wh=energy_wh, finished_at=now(), breakdown=json.dumps(breakdown))
            if s["payment_method"] == "manual":
                # No payment; the breakdown records what it would have cost.
                self.db.update_session(sid, status="COMPLETED", amount_charged="0.00")
                self.db.log(sid, "completed", f"{energy_wh} Wh, value £{cost}, not charged")
                return self.db.session(sid)
            if cost == 0:
                return self._void(s, "no energy delivered")
            try:
                cap = self.paypal.capture_authorization(
                    s["paypal_authorization_id"], amount=cost,
                    invoice_id=f"{self.settings.invoice_prefix}{sid}", request_id=f"capture-{sid}",
                )
            except PayPalError as e:
                self._fail_or_retry(sid, e, f"capture {cost}")
                raise PaymentError(502, "PayPal capture failed") from e
            status = {"COMPLETED": "CAPTURED", "PENDING": "CAPTURE_PENDING"}.get(cap.get("status"), "FAILED")
            self.db.update_session(
                sid, status=status, paypal_capture_id=cap.get("id"), amount_charged=f"{cost:.2f}",
                paypal_fee=_fee(cap),
                error=None if status != "FAILED" else f"capture status {cap.get('status')}",
            )
            self.db.log(sid, "captured", f"{cost:.2f} {cap.get('id')} {cap.get('status')}")
            return self.db.session(sid)

    def cancel(self, sid: str, reason: str) -> dict:
        """Release the hold without charging (car never plugged in, fault, ...)."""
        with self._lock:
            s = self._get(sid)
            if s["status"] == "CREATED":
                self.db.update_session(sid, status="ABANDONED", error=reason)
                return self.db.session(sid)
            if s["status"] not in ACTIVE:
                return s
            if s["payment_method"] == "manual":
                self.db.update_session(sid, status="CANCELLED", error=reason)
                self.db.log(sid, "cancelled", reason)
                return self.db.session(sid)
            return self._void(s, reason)

    def expire_unused(self, at: datetime | None = None) -> None:
        """Run every minute or so."""
        at = at or datetime.now(timezone.utc)
        limit = timedelta(minutes=self.settings.unused_auth_minutes)
        for s in self.db.sessions_with_status("AUTHORISED"):
            if _age(s["authorized_at"], at) > limit:
                try:
                    self.cancel(s["id"], f"not started within {self.settings.unused_auth_minutes} min")
                except PaymentError:
                    log.exception("voiding unused session %s", s["id"])
        for s in self.db.sessions_with_status("CREATED"):
            if _age(s["created_at"], at) > ABANDON_AFTER:
                self.cancel(s["id"], "never approved")

    # -- PayPal webhooks ---------------------------------------------------

    def handle_webhook(self, event: dict) -> str:
        """Reconcile with what PayPal says happened. Returns a note for the log."""
        etype = event.get("event_type", "")
        res = event.get("resource") or {}
        if not self.db.record_webhook(event.get("id", ""), etype, res.get("id")):
            return "duplicate"
        with self._lock:
            if etype.startswith("PAYMENT.CAPTURE."):
                s = self._session_for_capture(res)
                if not s:
                    return "unmatched capture"
                self.db.log(s["id"], etype.lower(), f"{res.get('id')} {res.get('status')}")
                if etype == "PAYMENT.CAPTURE.COMPLETED" and s["status"] in ("CAPTURE_PENDING", *ACTIVE):
                    self.db.update_session(s["id"], status="CAPTURED", paypal_capture_id=res.get("id"),
                                           **({"paypal_fee": _fee(res)} if _fee(res) else {}))
                elif etype in ("PAYMENT.CAPTURE.DENIED", "PAYMENT.CAPTURE.DECLINED"):
                    self.db.update_session(s["id"], status="FAILED", error=etype)
                # REFUNDED / REVERSED: logged above; refunds are handled in PayPal by the site owner.
                return etype
            if etype.startswith("PAYMENT.AUTHORIZATION."):
                s = self.db.session_by_authorization(res.get("id", ""))
                if not s:
                    return "unmatched authorization"
                self.db.log(s["id"], etype.lower(), str(res.get("status")))
                if etype == "PAYMENT.AUTHORIZATION.VOIDED" and s["status"] in ACTIVE:
                    # Voided outside this service (PayPal dashboard, or expired).
                    self.db.update_session(s["id"], status="VOIDED", error="voided at PayPal")
                elif etype == "PAYMENT.AUTHORIZATION.CREATED" and s["status"] == "FAILED":
                    # A PENDING hold that cleared after we told the driver no: release it.
                    try:
                        self.paypal.void_authorization(res["id"], request_id=f"void-{s['id']}")
                        self.db.log(s["id"], "voided", "late authorization released")
                    except PayPalError as e:
                        self.db.log(s["id"], "void_failed", f"{e} {e.body}")
                return etype
        return "ignored"

    # -- helpers -----------------------------------------------------------

    def _description(self, connector: str, sid: str) -> str:
        """Shown by PayPal on the payment. With PUBLIC_URL, it carries the driver's
        link back to their session, so nothing about them needs storing here."""
        text = f"EV charging, socket {connector}"
        if self.settings.public_url:
            linked = f"{text}. Progress: {self.settings.public_url}/pay/session/{sid}"
            if len(linked) <= 127:      # PayPal's limit for purchase_units[].description
                return linked
        return text

    def _get(self, sid: str) -> dict:
        s = self.db.session(sid)
        if not s:
            raise PaymentError(404, "Unknown session")
        return s

    def _session_for_capture(self, res: dict) -> dict | None:
        invoice = res.get("invoice_id") or ""
        if invoice.startswith(self.settings.invoice_prefix):
            s = self.db.session(invoice.removeprefix(self.settings.invoice_prefix))
            if s:
                return s
        auth_id = ((res.get("supplementary_data") or {}).get("related_ids") or {}).get("authorization_id")
        return self.db.session_by_authorization(auth_id) if auth_id else None

    def _void(self, s: dict, reason: str) -> dict:
        try:
            self.paypal.void_authorization(s["paypal_authorization_id"], request_id=f"void-{s['id']}")
        except PayPalError as e:
            self._fail_or_retry(s["id"], e, "void")
            raise PaymentError(502, "PayPal void failed") from e
        self.db.update_session(s["id"], status="VOIDED", amount_charged="0.00", error=reason)
        self.db.log(s["id"], "voided", reason)
        return self.db.session(s["id"])

    def _fail_or_retry(self, sid: str, e: PayPalError, step: str) -> None:
        """A 5xx leaves the session as it was so the same call can be retried
        (the PayPal-Request-Id makes the retry safe); a 4xx is final."""
        if e.status >= 500:
            log.error("session %s: %s: %s, retryable", sid, step, e)
            self.db.update_session(sid, error=f"{step}: {e} (retryable)")
            self.db.log(sid, "retryable", f"{step}: {e} {e.body}")
        else:
            self._fail(sid, f"{step}: {e} {e.body}")

    def _fail(self, sid: str, error: str) -> None:
        log.error("session %s: %s", sid, error)
        self.db.update_session(sid, status="FAILED", error=error[:1000])
        self.db.log(sid, "failed", error)
