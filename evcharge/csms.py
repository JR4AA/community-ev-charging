"""OCPP central system for the charger: 1.6J here, 2.0.1 in csms201.py, whichever
the charger asks for when it connects.

The charger connects to ws://<host>:9000/<charge point id> (also accepted:
/ocpp/<id>, which is the path the tunnel would use). Flow for one charge:

    driver plugs in         StatusNotification Preparing  -> pay page offers payment
    payment authorised      RemoteStartTransaction(idTag = session's tag)
    charger starts          StartTransaction  -> session CHARGING, transaction id
    every minute            MeterValues       -> running cost; RemoteStop near the hold
    unplug / Stop / limit   StopTransaction   -> settle: capture cost, release the rest

Access control lives here, not at Cloudflare: only known charge point ids, only
OCPP 1.6 or 2.0.1, encrypted (wss:// through the tunnel; plain ws:// only directly from a
private address, for chargers flagged as unable to do wss://), a per-charger password (HTTP Basic) whenever one is
configured and always for connections arriving through the tunnel, and a
lockout after repeated wrong passwords.
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import ipaddress
import json
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from http import HTTPStatus

import websockets
from ocpp.routing import on
from ocpp.v16 import ChargePoint as OcppChargePoint
from ocpp.v16 import call, call_result
from ocpp.v16.datatypes import IdTagInfo
from ocpp.v16.enums import (
    Action,
    AuthorizationStatus,
    DataTransferStatus,
    MessageTrigger,
    RegistrationStatus,
    RemoteStartStopStatus,
)
from websockets.asyncio.server import ServerConnection
from websockets.http11 import Request

from .config import Settings
from .db import DB
from .payments import PaymentError, Payments, session_tariff

log = logging.getLogger(__name__)

SUBPROTOCOL = "ocpp1.6"
SUBPROTOCOLS = ["ocpp2.0.1", SUBPROTOCOL]     # the charger's preference wins when it offers both
COSTMSG = "org.openchargealliance.costmsg"   # OCA "California pricing" DataTransfer vendor id
ENERGY = "Energy.Active.Import.Register"
OFFLINE = "Offline"
LOCKOUT_FAILURES = 5
LOCKOUT_SECONDS = 15 * 60
CONFIGURATION = {
    "MeterValueSampleInterval": "60",
    "MeterValuesSampledData": ENERGY,
    "StopTransactionOnInvalidId": "true",
}


def utc_iso(ts: str | None) -> str:
    """Charger timestamp -> UTC ISO string in db.now()'s format, so strings sort in time order."""
    if not ts:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def energy_readings(meter_values: list[dict]) -> list[tuple[str, int]]:
    """[(UTC ISO time, Wh)] for the total imported energy in a MeterValues payload."""
    out = []
    for mv in meter_values or []:
        at = utc_iso(mv.get("timestamp"))
        for sv in mv.get("sampled_value", []):
            if sv.get("measurand", ENERGY) != ENERGY or sv.get("phase") or sv.get("location", "Outlet") != "Outlet":
                continue
            # 1.6: "value" is a string, "unit" alongside; 2.0.1: a number, with unitOfMeasure {unit, multiplier}.
            uom = sv.get("unit_of_measure") or {}
            value = Decimal(str(sv["value"])).scaleb(uom.get("multiplier", 0))
            wh = value * 1000 if (sv.get("unit") or uom.get("unit") or "Wh") == "kWh" else value
            out.append((at, int(wh)))
    return out


class ChargePoint(OcppChargePoint):
    version = "1.6"

    def __init__(self, cp_id: str, connection, csms: CSMS):
        super().__init__(cp_id, connection)
        self.csms = csms

    @property
    def db(self) -> DB:
        return self.csms.db

    async def route_message(self, raw_msg):
        self.csms.info.setdefault(self.id, {})["last_seen"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return await super().route_message(raw_msg)

    async def configure(self) -> None:
        """Ask for energy readings every minute, then for the current connector states."""
        await asyncio.sleep(1)
        try:
            for key, value in CONFIGURATION.items():
                r = await self.call(call.ChangeConfiguration(key=key, value=value))
                log.info("%s: %s=%s -> %s", self.id, key, value, r.status)
            await self.call(call.TriggerMessage(requested_message=MessageTrigger.status_notification))
        except Exception:
            log.exception("%s: configuring failed", self.id)
        try:
            await self.csms.read_configuration(self.id)
            await self.csms.setup_price_display(self.id)
        except Exception:
            log.exception("%s: reading settings / price display setup failed", self.id)

    @on(Action.boot_notification)
    def on_boot(self, charge_point_vendor, charge_point_model, **kw):
        log.info("%s: boot %s %s firmware %s", self.id, charge_point_vendor, charge_point_model,
                 kw.get("firmware_version"))
        self.csms.info.setdefault(self.id, {}).update(
            vendor=charge_point_vendor, model=charge_point_model, firmware=kw.get("firmware_version"),
            serial=kw.get("charge_point_serial_number") or kw.get("charge_box_serial_number"))
        return call_result.BootNotification(
            current_time=datetime.now(timezone.utc).isoformat(), interval=300, status=RegistrationStatus.accepted,
        )

    @on(Action.heartbeat)
    def on_heartbeat(self):
        return call_result.Heartbeat(current_time=datetime.now(timezone.utc).isoformat())

    @on(Action.status_notification)
    async def on_status(self, connector_id, error_code, status, **kw):
        log.info("%s/%s: %s (%s)", self.id, connector_id, status, error_code)
        await self.csms.connector_status(self.id, connector_id, status)
        return call_result.StatusNotification()

    @on(Action.authorize)
    def on_authorize(self, id_tag):
        s = self.db.session_by_id_tag(id_tag)
        ok = bool(s and s["status"] == "AUTHORISED" and self.csms.settings.sockets.get(s["connector"], ("",))[0] == self.id)
        return call_result.Authorize(id_tag_info=IdTagInfo(
            status=AuthorizationStatus.accepted if ok else AuthorizationStatus.invalid))

    @on(Action.start_transaction)
    async def on_start_transaction(self, connector_id, id_tag, meter_start, timestamp, **kw):
        at = utc_iso(timestamp)
        s = self.db.session_by_id_tag(id_tag)
        ok = bool(s and s["status"] == "AUTHORISED"
                  and self.csms.settings.sockets.get(s["connector"]) == (self.id, connector_id))
        tx = self.db.start_transaction(s["id"] if ok else None, self.id, connector_id, id_tag, meter_start, at)
        if ok:
            await asyncio.to_thread(self.csms.payments.mark_charging, s["id"])
            self.db.log(s["id"], "transaction_started", f"tx {tx} meter {meter_start} Wh")
            asyncio.create_task(self.csms.send_running_cost(tx, delay=0.5))
        else:
            log.warning("%s/%s: refused transaction for idTag %s", self.id, connector_id, id_tag)
        return call_result.StartTransaction(transaction_id=tx, id_tag_info=IdTagInfo(
            status=AuthorizationStatus.accepted if ok else AuthorizationStatus.invalid))

    @on(Action.meter_values)
    async def on_meter_values(self, connector_id, meter_value, transaction_id=None, **kw):
        t = self.db.transaction(transaction_id) if transaction_id else None
        if t and t["session_id"] and not t["stopped_at"]:
            for at, wh in energy_readings(meter_value):
                self.db.add_reading(t["id"], at, wh)
            await self.csms.check_limit(t)
            asyncio.create_task(self.csms.send_running_cost(t["id"]))
        return call_result.MeterValues()

    @on(Action.stop_transaction)
    async def on_stop_transaction(self, meter_stop, timestamp, transaction_id, **kw):
        t = self.db.transaction(transaction_id)
        if t and t["session_id"]:
            for at, wh in energy_readings(kw.get("transaction_data") or []):
                self.db.add_reading(t["id"], at, wh)
            self.db.stop_transaction(t["id"], meter_stop, utc_iso(timestamp), kw.get("reason"))
            self.db.log(t["session_id"], "transaction_stopped", f"tx {t['id']} meter {meter_stop} Wh {kw.get('reason')}")
            # Answer the charger first; settling talks to PayPal.
            asyncio.create_task(self.csms.settle(t["id"], final_cost=True))
        return call_result.StopTransaction(id_tag_info=IdTagInfo(status=AuthorizationStatus.accepted))

    @on(Action.data_transfer)
    def on_data_transfer(self, vendor_id, **kw):
        return call_result.DataTransfer(status=DataTransferStatus.unknown_vendor_id)

    @on(Action.firmware_status_notification)
    def on_firmware_status(self, status):
        log.info("%s: firmware %s", self.id, status)
        return call_result.FirmwareStatusNotification()

    @on(Action.diagnostics_status_notification)
    def on_diagnostics_status(self, status):
        return call_result.DiagnosticsStatusNotification()


    # -- what CSMS asks of a charger, in 1.6 terms (csms201.py has the 2.0.1 versions) --

    async def remote_start(self, session: dict, connector: int) -> str:
        r = await self.call(call.RemoteStartTransaction(id_tag=session["id_tag"], connector_id=connector))
        return str(r.status)

    async def remote_stop(self, t: dict) -> str:
        r = await self.call(call.RemoteStopTransaction(transaction_id=t["id"]))
        return str(r.status)

    async def read_settings(self) -> tuple[list[dict], list[str]]:
        """GetConfiguration: every setting, as [{key, value, readonly}], and the unknown keys."""
        r = await self.call(call.GetConfiguration())
        return r.configuration_key or [], r.unknown_key or []

    async def setup_display(self) -> bool:
        """Turn on the OCA 'California pricing' screen messages if the charger has them
        (it reports CustomDisplayCostAndPrice), and give it the tariff to show while idle."""
        flag = self.csms._key(self.id, "CustomDisplayCostAndPrice")
        if not flag:
            log.info("%s: no CustomDisplayCostAndPrice, so no prices on its screen", self.id)
            return False
        if str(flag.get("value", "")).lower() != "true":
            if flag.get("readonly"):
                return False
            r = await self.call(call.ChangeConfiguration(key="CustomDisplayCostAndPrice", value="true"))
            if r.status != "Accepted":
                log.warning("%s: enabling the price display -> %s", self.id, r.status)
                return False
        if self.csms._key(self.id, "DefaultPrice"):
            text = self.csms.settings.tariff.describe()
            r = await self.call(call.ChangeConfiguration(key="DefaultPrice", value=json.dumps(
                {"priceText": text, "priceTextOffline": "Charger offline: charging is not available."})))
            log.info("%s: DefaultPrice %r -> %s", self.id, text, r.status)
        return True


class CSMS:
    def __init__(self, settings: Settings, db: DB, payments: Payments):
        self.settings = settings
        self.db = db
        self.payments = payments
        self.points: dict[str, ChargePoint] = {}     # or csms201.ChargePoint201
        self.status: dict[tuple[str, int], str] = {}
        # Per charge point: vendor, model, firmware, serial (from BootNotification), last_seen.
        self.info: dict[str, dict] = {}
        self.config: dict[str, dict] = {}         # last GetConfiguration per charge point
        self.display: dict[str, bool] = {}        # price messages on its screen?
        self.last_display: dict[str, dict] = {}   # last price message and the charger's answer
        self._failures: dict[str, list[float]] = {}
        self._stopping: set[int] = set()

    # -- for the pay pages ---------------------------------------------------

    def socket_state(self, socket: str) -> str:
        cp, conn = self.settings.sockets[socket]
        if cp not in self.points:
            return OFFLINE
        return self.status.get((cp, conn), "Unknown")

    async def remote_start(self, session: dict) -> bool:
        cp_id, conn = self.settings.sockets[session["connector"]]
        cp = self.points.get(cp_id)
        if not cp:
            return False
        try:
            status = await cp.remote_start(session, conn)
        except Exception:
            log.exception("%s: remote start failed", cp_id)
            return False
        self.db.log(session["id"], "remote_start", status)
        return status == RemoteStartStopStatus.accepted

    async def remote_stop(self, session_id: str, why: str) -> bool:
        t = self.db.transaction_for_session(session_id)
        if not t or t["stopped_at"]:
            return False
        cp = self.points.get(t["charge_point"])
        if not cp:
            return False
        try:
            status = await cp.remote_stop(t)
        except Exception:
            log.exception("%s: remote stop failed", t["charge_point"])
            return False
        self.db.log(session_id, "remote_stop", f"{why}: {status}")
        return status == RemoteStartStopStatus.accepted

    async def connector_status(self, cp_id: str, connector: int, status: str) -> None:
        self.status[(cp_id, connector)] = status
        socket = self.settings.socket_for(cp_id, connector)
        if socket and status == "Available":
            # Unplugged before charging started: release the driver's hold.
            for s in self.db.sessions_with_status("AUTHORISED", connector=socket):
                await asyncio.to_thread(self.payments.cancel, s["id"], "car unplugged before charging started")

    # -- money ---------------------------------------------------------------

    async def check_limit(self, t: dict) -> None:
        """Stop the charger before the cost reaches the hold.

        Handlers must never await a call to the same charger (deadlock): spawn it."""
        s = self.db.session(t["session_id"])
        readings = self.db.readings(t["id"])
        cost, _ = self.payments.running_cost(s["id"], readings)
        if s["limit_wh"]:
            delivered = readings[-1][1] - readings[0][1]
            reached = delivered >= s["limit_wh"] - self.settings.stop_margin_wh
        else:
            reached = cost >= Decimal(s["hold_amount"]) - self.settings.stop_margin
        if reached and t["id"] not in self._stopping:
            self._stopping.add(t["id"])
            # Not awaited: we're inside the charger's MeterValues handler, and the
            # library reads the charger's reply to our call only after it returns.
            asyncio.create_task(self.remote_stop(s["id"], f"limit reached (£{cost})"))

    async def settle(self, tx: int, final_cost: bool = False) -> None:
        t = self.db.transaction(tx)
        try:
            await asyncio.to_thread(self.payments.settle, t["session_id"], readings=self.db.readings(tx))
        except PaymentError:
            log.exception("settling tx %s failed; will retry", tx)
        if final_cost:
            await self.send_final_cost(tx)

    # -- charger settings and its screen ------------------------------------

    async def read_configuration(self, cp_id: str) -> list[dict]:
        """Every setting the charger reports. Kept for the admin page."""
        cp = self.points.get(cp_id)
        if not cp:
            raise PaymentError(503, f"{cp_id} is offline")
        keys, unknown = await cp.read_settings()
        keys = sorted(keys, key=lambda k: k["key"])
        self.config[cp_id] = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                              "keys": keys, "unknown": unknown}
        return keys

    def _key(self, cp_id: str, name: str) -> dict | None:
        return next((k for k in self.config.get(cp_id, {}).get("keys", []) if k["key"] == name), None)

    async def setup_price_display(self, cp_id: str) -> None:
        """Prices on the charger's screen, if it can show them and DISPLAY_PRICES allows."""
        self.display[cp_id] = False
        if self.settings.display_prices == "off":
            return
        self.display[cp_id] = await self.points[cp_id].setup_display()

    def screen_cost(self, tx: int, final: bool = False) -> tuple[Decimal, Decimal, str]:
        """(cost so far, price now, one line for the screen) for a transaction."""
        t = self.db.transaction(tx)
        s = self.db.session(t["session_id"])
        readings = self.db.readings(tx)
        kwh = (readings[-1][1] - readings[0][1]) / 1000 if readings else 0
        if s["payment_method"] == "manual":
            return Decimal(0), Decimal(0), f"{kwh:.2f} kWh. No charge." + (" Thank you!" if final else "")
        cost = self.payments.running_cost(s["id"], readings)[0] if len(readings) > 1 else Decimal(0)
        tariff = session_tariff(s)
        at = readings[-1][0] if readings else datetime.now(timezone.utc)
        solar = self.db.solar_active() and tariff.solar_price is not None
        price = tariff.solar_price if solar else tariff.band_at(at).price
        if final:
            return cost, price, f"{kwh:.2f} kWh, total £{cost:.2f}" + (" (no energy, nothing taken)" if cost == 0 else "")
        return cost, price, f"{kwh:.2f} kWh, £{cost:.2f} so far. Now {price * 100:.0f}p/kWh."

    async def _cost_message(self, cp_id: str, message_id: str, data: dict) -> None:
        cp = self.points.get(cp_id)
        if not cp or not self.display.get(cp_id) or cp.version != "1.6":
            return      # 2.0.1 gets its costs in TransactionEvent responses instead
        try:
            r = await cp.call(call.DataTransfer(vendor_id=COSTMSG, message_id=message_id, data=json.dumps(data)))
            self.last_display[cp_id] = {"message": message_id, "status": str(r.status),
                                        "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        except Exception:
            log.exception("%s: %s failed", cp_id, message_id)

    async def send_running_cost(self, tx: int, delay: float = 0) -> None:
        if delay:
            await asyncio.sleep(delay)      # "immediately after" StartTransaction.conf, not before it
        t = self.db.transaction(tx)
        if not t or not t["session_id"] or t["stopped_at"] or not self.display.get(t["charge_point"]):
            return
        s = self.db.session(t["session_id"])
        readings = self.db.readings(tx)
        free = s["payment_method"] == "manual"
        tariff = session_tariff(s)
        cost = Decimal(0) if free or len(readings) < 2 else self.payments.running_cost(s["id"], readings)[0]
        at, wh = readings[-1]
        solar = self.db.solar_active() and tariff.solar_price is not None
        price = Decimal(0) if free else (tariff.solar_price if solar else tariff.band_at(at).price)
        data = {"transactionId": tx, "timestamp": at.isoformat(), "meterValue": wh, "cost": float(cost),
                "state": "Charging",
                "chargingPrice": {"kWhPrice": float(price), "flatFee": 0.0 if free else float(tariff.session_fee)}}
        nxt = None if free or solar else tariff.next_change(at)
        if nxt:
            data["nextPeriod"] = {"atTime": nxt[0].isoformat(), "chargingPrice": {"kWhPrice": float(nxt[1].price)}}
            data["triggerMeterValue"] = {"atTime": nxt[0].isoformat()}
        await self._cost_message(t["charge_point"], "RunningCost", data)

    async def send_final_cost(self, tx: int) -> None:
        t = self.db.transaction(tx)
        if not t or not t["session_id"] or not self.display.get(t["charge_point"]):
            return
        s = self.db.session(t["session_id"])
        kwh = (s["energy_wh"] or 0) / 1000
        if s["payment_method"] == "manual":
            cost, text = Decimal(0), f"{kwh:.2f} kWh. No charge. Thank you!"
        else:
            cost = Decimal(s["amount_charged"] or 0)
            text = f"{kwh:.2f} kWh, total £{cost:.2f}" + (" (no energy, nothing taken)" if cost == 0 else "")
        data = {"transactionId": tx, "cost": float(cost), "priceText": text}
        if self.settings.public_url:
            data["qrCodeText"] = f"{self.settings.public_url}/pay/session/{s['id']}"
            data["priceText"] += " Scan for your receipt."
        await self._cost_message(t["charge_point"], "FinalCost", data)

    def retry_settlements(self) -> None:
        """Run periodically: settle anything stopped whose payment failed (PayPal down, restart)."""
        for t in self.db.stopped_unsettled():
            try:
                self.payments.settle(t["session_id"], readings=self.db.readings(t["id"]))
            except PaymentError:
                log.warning("retry settling tx %s failed", t["id"])

    # -- connections ---------------------------------------------------------

    async def serve(self, host: str = "0.0.0.0", port: int | None = None):
        return await websockets.serve(
            self._handler, host, self.settings.ocpp_port if port is None else port,
            subprotocols=SUBPROTOCOLS, select_subprotocol=_pick_subprotocol,
            process_request=self._check, server_header=None,
        )

    def _check(self, connection: ServerConnection, request: Request):
        cp_id = request.path.rstrip("/").rsplit("/", 1)[-1]
        if cp_id not in self.settings.charge_points:
            return connection.respond(HTTPStatus.NOT_FOUND, "Unknown charge point\n")
        protocols = request.headers.get("Sec-WebSocket-Protocol", "")
        if not set(SUBPROTOCOLS) & {p.strip() for p in protocols.split(",")}:
            return connection.respond(HTTPStatus.BAD_REQUEST, "OCPP 1.6 or 2.0.1 only\n")
        tunnel_ip = request.headers.get("Cf-Connecting-Ip")
        source = tunnel_ip or str(connection.remote_address[0])
        # Encrypted by default: wss:// through the tunnel. Plain ws:// only directly
        # from a private address, and only for a charger that can't do wss://
        # (OCPP_ALLOW_WS_<id>).
        if tunnel_ip:
            if _cf_scheme(request.headers) != "https":
                return connection.respond(HTTPStatus.FORBIDDEN, "Use wss://\n")
        elif cp_id not in self.settings.ocpp_allow_ws or not _is_private(source):
            log.warning("%s: plain ws:// from %s refused (allowed: %s, private: %s)", cp_id, source,
                        cp_id in self.settings.ocpp_allow_ws, _is_private(source))
            return connection.respond(HTTPStatus.FORBIDDEN, "Use wss://\n")
        if self._locked(source):
            return connection.respond(HTTPStatus.TOO_MANY_REQUESTS, "Too many attempts\n")
        password = self.settings.ocpp_passwords.get(cp_id)
        if tunnel_ip and not password:
            log.warning("%s: tunnel connection from %s refused: no password configured", cp_id, source)
            return connection.respond(HTTPStatus.FORBIDDEN, "Forbidden\n")
        if password and not _basic_auth_ok(request.headers.get("Authorization", ""), cp_id, password):
            # Only a wrong password counts towards the lockout: a charger set up without
            # one would otherwise lock out every charger behind the same public address.
            if request.headers.get("Authorization"):
                self._failed(source)
            log.warning("%s: bad credentials from %s", cp_id, source)
            r = connection.respond(HTTPStatus.UNAUTHORIZED, "Unauthorized\n")
            r.headers["WWW-Authenticate"] = 'Basic realm="ocpp"'
            return r
        return None

    def _locked(self, source: str) -> bool:
        now = time.monotonic()
        recent = [t for t in self._failures.get(source, []) if now - t < LOCKOUT_SECONDS]
        self._failures[source] = recent
        return len(recent) >= LOCKOUT_FAILURES

    def _failed(self, source: str) -> None:
        self._failures.setdefault(source, []).append(time.monotonic())

    async def _handler(self, connection: ServerConnection) -> None:
        cp_id = connection.request.path.rstrip("/").rsplit("/", 1)[-1]
        old = self.points.get(cp_id)
        if old:
            log.info("%s: reconnected, dropping the old connection", cp_id)
            await old._connection.close()
        if connection.subprotocol == SUBPROTOCOL:
            cp = ChargePoint(cp_id, connection, self)
        else:
            from .csms201 import ChargePoint201     # here, not at the top: it imports this module
            cp = ChargePoint201(cp_id, connection, self)
        self.points[cp_id] = cp
        self.info.setdefault(cp_id, {})["protocol"] = f"OCPP {cp.version}"
        log.info("%s: connected from %s over OCPP %s", cp_id, connection.request.headers.get("Cf-Connecting-Ip")
                 or connection.remote_address[0], cp.version)
        configure = asyncio.create_task(cp.configure())
        try:
            await cp.start()
        except websockets.ConnectionClosed:
            pass
        finally:
            configure.cancel()
            if self.points.get(cp_id) is cp:
                del self.points[cp_id]
            log.info("%s: disconnected", cp_id)


def _pick_subprotocol(connection: ServerConnection, offered) -> str | None:
    """The first of the charger's offered OCPP versions that we speak (websockets would pick ours)."""
    return next((p for p in offered if p in SUBPROTOCOLS), None)


PRIVATE_NETS = [ipaddress.ip_network(n) for n in
                ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "::1/128")]


def _is_private(address: str) -> bool:
    """RFC 1918 or loopback (the simulator, and Docker's own bridge)."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return any(ip in net for net in PRIVATE_NETS)


def _cf_scheme(headers) -> str:
    """The scheme the client used to reach Cloudflare."""
    try:
        return json.loads(headers.get("Cf-Visitor", "{}")).get("scheme", "")
    except ValueError:
        return ""


def _basic_auth_ok(header: str, user: str, password: str) -> bool:
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        got_user, _, got_password = base64.b64decode(value, validate=True).decode().partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return hmac.compare_digest(got_user.encode(), user.encode()) & hmac.compare_digest(got_password.encode(), password.encode())
