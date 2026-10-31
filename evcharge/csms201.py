"""OCPP 2.0.1 chargers: the same flow as 1.6 (csms.py), in 2.0.1's messages.

    driver plugs in         StatusNotification Occupied  -> shown as Preparing: pay page offers payment
    payment authorised      RequestStartTransaction(idToken = session's tag, remoteStartId)
    charger starts          TransactionEvent with that idToken -> session CHARGING
    every minute            TransactionEvent Updated with meter values -> running cost; RequestStop near the hold
    unplug / Stop / limit   TransactionEvent Ended -> settle: capture cost, release the rest

The charger numbers transactions itself (strings), and may start one as soon as
the cable goes in, before anyone has paid. Such a transaction is only followed in
memory until a payment's idToken arrives on it; it then gets a row in
transactions, starting from the meter reading at that moment, so nothing before
the payment is billed.

Prices on the screen use the standard TariffCostCtrlr, if the charger has it: cost
so far and a line of text go back in each TransactionEvent response.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timezone

from ocpp.routing import on
from ocpp.v201 import ChargePoint as OcppChargePoint
from ocpp.v201 import call, call_result
from ocpp.v201.enums import Action

from .csms import CSMS, ENERGY, energy_readings, utc_iso
from .db import DB
from .payments import PaymentError

log = logging.getLogger(__name__)

# (component, variable, instance, value): meter readings every minute, and at the
# start and end of each transaction; stop a transaction whose idToken we refuse.
CONFIGURATION = [
    ("SampledDataCtrlr", "TxUpdatedInterval", None, "60"),
    ("SampledDataCtrlr", "TxUpdatedMeasurands", None, ENERGY),
    ("SampledDataCtrlr", "TxStartedMeasurands", None, ENERGY),
    ("SampledDataCtrlr", "TxEndedMeasurands", None, ENERGY),
    ("TxCtrlr", "StopTxOnInvalidId", None, "true"),
]
# 2.0.1 charging states, in the 1.6 connector states the pay pages understand.
STATES = {"Charging": "Charging", "SuspendedEV": "SuspendedEV", "SuspendedEVSE": "SuspendedEVSE",
          "EVConnected": "SuspendedEV", "Idle": "SuspendedEVSE"}
REPORT_TIMEOUT = 60


def variable_name(component: dict, variable: dict) -> str:
    """Component[instance].Variable[instance], with the EVSE when the component has one."""
    name = component["name"] + (f"[{component['instance']}]" if component.get("instance") else "")
    if component.get("evse"):
        name += f"(EVSE {component['evse']['id']})"
    return f"{name}.{variable['name']}" + (f"[{variable['instance']}]" if variable.get("instance") else "")


class ChargePoint201(OcppChargePoint):
    version = "2.0.1"

    def __init__(self, cp_id: str, connection, csms: CSMS):
        super().__init__(cp_id, connection)
        self.csms = csms
        self.tx_evse: dict[str, int] = {}                  # charger's transaction id -> EVSE
        self.meter: dict[str, tuple[str, int]] = {}        # its latest energy reading (UTC ISO, Wh)
        self.waiting: dict[str, str] = {}                  # paid for, no meter reading yet -> session id
        self.remote: dict[int, str] = {}                   # remoteStartId -> session id
        self.reports: dict[int, list] = {}
        self.report_done: dict[int, asyncio.Event] = {}
        self._request_id = 0

    @property
    def db(self) -> DB:
        return self.csms.db

    async def route_message(self, raw_msg):
        self.csms.info.setdefault(self.id, {})["last_seen"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return await super().route_message(raw_msg)

    async def configure(self) -> None:
        await asyncio.sleep(1)
        try:
            for name, status in (await self.set_variables(CONFIGURATION)).items():
                log.info("%s: %s -> %s", self.id, name, status)
            for p, evse in self.csms.settings.sockets.values():
                if p == self.id:
                    await self.call(call.TriggerMessage(requested_message="StatusNotification",
                                                        evse={"id": evse, "connector_id": 1}))
        except Exception:
            log.exception("%s: configuring failed", self.id)
        try:
            await self.csms.read_configuration(self.id)
            await self.csms.setup_price_display(self.id)
        except Exception:
            log.exception("%s: reading settings / price display setup failed", self.id)

    async def set_variables(self, items) -> dict[str, str]:
        r = await self.call(call.SetVariables(set_variable_data=[
            {"attribute_value": value, "component": {"name": component},
             "variable": {"name": variable, **({"instance": instance} if instance else {})}}
            for component, variable, instance, value in items]))
        return {variable_name(x["component"], x["variable"]): x["attribute_status"] for x in r.set_variable_result}

    # -- what CSMS asks of a charger -------------------------------------------

    async def remote_start(self, session: dict, connector: int) -> str:
        rid = secrets.randbelow(2**31)
        self.remote[rid] = session["id"]
        r = await self.call(call.RequestStartTransaction(
            id_token={"id_token": session["id_tag"], "type": "Central"}, remote_start_id=rid, evse_id=connector))
        if r.status == "Accepted" and r.transaction_id:     # a transaction already running since plug-in
            self.waiting[r.transaction_id] = session["id"]
        return str(r.status)

    async def remote_stop(self, t: dict) -> str:
        r = await self.call(call.RequestStopTransaction(transaction_id=t["ocpp_tx_id"]))
        return str(r.status)

    async def read_settings(self) -> tuple[list[dict], list[str]]:
        """GetBaseReport FullInventory; the charger answers in NotifyReport messages."""
        self._request_id += 1
        rid = self._request_id
        self.reports[rid], self.report_done[rid] = [], asyncio.Event()
        try:
            r = await self.call(call.GetBaseReport(request_id=rid, report_base="FullInventory"))
            if r.status != "Accepted":
                raise PaymentError(502, f"{self.id} answered {r.status}")
            await asyncio.wait_for(self.report_done[rid].wait(), REPORT_TIMEOUT)
            data = self.reports[rid]
        finally:
            self.reports.pop(rid, None)
            self.report_done.pop(rid, None)
        keys = []
        for d in data:
            name = variable_name(d["component"], d["variable"])
            for a in d.get("variable_attribute", []):
                kind = a.get("type", "Actual")
                keys.append({"key": name if kind == "Actual" else f"{name} ({kind})", "value": a.get("value", ""),
                             "readonly": a.get("mutability") == "ReadOnly"})
        return keys, []

    async def setup_display(self) -> bool:
        if not any(k["key"].startswith("TariffCostCtrlr") for k in self.csms.config.get(self.id, {}).get("keys", [])):
            log.info("%s: no TariffCostCtrlr, so no prices on its screen", self.id)
            return False
        settings = self.csms.settings
        result = await self.set_variables([
            ("TariffCostCtrlr", "Enabled", "Tariff", "true"),
            ("TariffCostCtrlr", "Enabled", "Cost", "true"),
            ("TariffCostCtrlr", "TariffFallbackMessage", None, settings.tariff.describe()),
            ("TariffCostCtrlr", "Currency", None, settings.currency),
        ])
        log.info("%s: price display %s", self.id, result)
        return result.get("TariffCostCtrlr.Enabled[Cost]") == "Accepted"

    # -- from the charger --------------------------------------------------------

    @on(Action.boot_notification)
    def on_boot(self, charging_station, reason, **kw):
        log.info("%s: boot (%s) %s %s firmware %s", self.id, reason, charging_station.get("vendor_name"),
                 charging_station.get("model"), charging_station.get("firmware_version"))
        self.csms.info.setdefault(self.id, {}).update(
            vendor=charging_station.get("vendor_name"), model=charging_station.get("model"),
            firmware=charging_station.get("firmware_version"), serial=charging_station.get("serial_number"))
        return call_result.BootNotification(
            current_time=datetime.now(timezone.utc).isoformat(), interval=300, status="Accepted")

    @on(Action.heartbeat)
    def on_heartbeat(self):
        return call_result.Heartbeat(current_time=datetime.now(timezone.utc).isoformat())

    @on(Action.status_notification)
    async def on_status(self, timestamp, connector_status, evse_id, connector_id, **kw):
        log.info("%s/%s: %s", self.id, evse_id, connector_status)
        state = connector_status
        if connector_status == "Occupied":
            # A car is plugged in: ready to pay, unless a paid charge is under way.
            current = self.csms.status.get((self.id, evse_id))
            state = (current if current in STATES.values() or current == "Finishing" else "Charging") \
                if self._paid_on(evse_id) else "Preparing"
        await self.csms.connector_status(self.id, evse_id, state)
        return call_result.StatusNotification()

    def _paid_on(self, evse_id: int) -> bool:
        return any(e == evse_id and (tx in self.waiting or self.db.transaction_by_ocpp(self.id, tx))
                   for tx, e in self.tx_evse.items())

    @on(Action.authorize)
    def on_authorize(self, id_token, **kw):
        s = self.db.session_by_id_tag(id_token["id_token"])
        ok = bool(s and s["status"] == "AUTHORISED" and self.csms.settings.sockets.get(s["connector"], ("",))[0] == self.id)
        return call_result.Authorize(id_token_info={"status": "Accepted" if ok else "Invalid"})

    @on(Action.transaction_event)
    async def on_transaction_event(self, event_type, timestamp, trigger_reason, seq_no, transaction_info,
                                   meter_value=None, evse=None, id_token=None, **kw):
        txid = transaction_info["transaction_id"]
        if evse:
            self.tx_evse[txid] = evse["id"]
        evse_id = self.tx_evse.get(txid)
        readings = energy_readings(meter_value)
        if readings:
            self.meter[txid] = readings[-1]
        ended = event_type == "Ended"
        tag = (id_token or {}).get("id_token")
        t = self.db.transaction_by_ocpp(self.id, txid)
        linked_now = False
        if t is None:
            s = self._session_for(txid, tag, transaction_info.get("remote_start_id"), evse_id)
            if s:
                self.waiting[txid] = s["id"]
            elif tag:
                log.warning("%s/%s: refused transaction %s for idToken %s", self.id, evse_id, txid, tag)
            if txid in self.waiting and txid in self.meter and not ended:
                t = await self._link(txid, evse_id)
                linked_now = True
        else:
            for at, wh in readings:
                self.db.add_reading(t["id"], at, wh)

        response = {}
        if tag:
            ok = (t and t["id_tag"] == tag) or (txid in self.waiting and self.db.session(self.waiting[txid])["id_tag"] == tag)
            response["id_token_info"] = {"status": "Accepted" if ok else "Invalid"}
        if ended:
            for d in (self.tx_evse, self.meter, self.waiting):
                d.pop(txid, None)
            if evse_id is not None:
                self.csms.status[(self.id, evse_id)] = "Finishing" if t else self.csms.status.get((self.id, evse_id), "Unknown")
            if t and not t["stopped_at"]:
                last = self.db.readings(t["id"])[-1]
                wh = readings[-1][1] if readings else last[1]
                self.db.stop_transaction(t["id"], wh, utc_iso(timestamp), transaction_info.get("stopped_reason"))
                self.db.log(t["session_id"], "transaction_stopped",
                            f"tx {t['id']} ({txid}) meter {wh} Wh {transaction_info.get('stopped_reason')}")
                self._screen(t["id"], response, final=True)
                asyncio.create_task(self.csms.settle(t["id"]))   # answer the charger first
            return call_result.TransactionEvent(**response)
        if t:
            if transaction_info.get("charging_state"):
                self.csms.status[(self.id, evse_id)] = STATES.get(transaction_info["charging_state"], "Charging")
            elif linked_now:
                self.csms.status[(self.id, evse_id)] = "Charging"
            if not linked_now:
                await self.csms.check_limit(t)
            self._screen(t["id"], response)
        return call_result.TransactionEvent(**response)

    def _session_for(self, txid: str, tag: str | None, remote_start_id: int | None, evse_id: int | None) -> dict | None:
        """The paid session this transaction is for, if it's ready to start on this EVSE."""
        if tag:
            s = self.db.session_by_id_tag(tag)
        else:
            sid = self.waiting.get(txid) or self.remote.get(remote_start_id)
            s = self.db.session(sid) if sid else None
        if s and s["status"] == "AUTHORISED" and self.csms.settings.sockets.get(s["connector"]) == (self.id, evse_id):
            return s
        return None

    async def _link(self, txid: str, evse_id: int) -> dict:
        """Start billing a transaction from its latest meter reading."""
        s = self.db.session(self.waiting.pop(txid))
        at, wh = self.meter[txid]
        tx = self.db.start_transaction(s["id"], self.id, evse_id, s["id_tag"], wh, at, ocpp_tx_id=txid)
        await asyncio.to_thread(self.csms.payments.mark_charging, s["id"])
        self.db.log(s["id"], "transaction_started", f"tx {tx} ({txid}) meter {wh} Wh")
        return self.db.transaction(tx)

    def _screen(self, tx: int, response: dict, final: bool = False) -> None:
        """Cost so far, and a line of text, for chargers showing prices (TariffCostCtrlr)."""
        if not self.csms.display.get(self.id):
            return
        cost, _, text = self.csms.screen_cost(tx, final)
        response["total_cost"] = float(cost)
        response["updated_personal_message"] = {"format": "UTF8", "content": text}
        self.csms.last_display[self.id] = {"message": "FinalCost" if final else "RunningCost", "status": "sent",
                                           "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    @on(Action.meter_values)
    def on_meter_values(self, evse_id, meter_value, **kw):
        return call_result.MeterValues()      # outside transactions: not billed

    @on(Action.notify_report)
    def on_notify_report(self, request_id, generated_at, seq_no, report_data=None, tbc=False, **kw):
        if request_id in self.reports:
            self.reports[request_id].extend(report_data or [])
            if not tbc:
                self.report_done[request_id].set()
        return call_result.NotifyReport()

    @on(Action.notify_event)
    def on_notify_event(self, generated_at, seq_no, event_data, **kw):
        for e in event_data:
            log.info("%s: event %s %s = %s", self.id, e.get("trigger"),
                     variable_name(e.get("component", {"name": "?"}), e.get("variable", {"name": "?"})), e.get("actual_value"))
        return call_result.NotifyEvent()

    @on(Action.security_event_notification)
    def on_security_event(self, type, timestamp, **kw):
        log.info("%s: security event %s", self.id, type)
        return call_result.SecurityEventNotification()

    @on(Action.firmware_status_notification)
    def on_firmware_status(self, status, **kw):
        log.info("%s: firmware %s", self.id, status)
        return call_result.FirmwareStatusNotification()

    @on(Action.log_status_notification)
    def on_log_status(self, status, **kw):
        return call_result.LogStatusNotification()

    @on(Action.data_transfer)
    def on_data_transfer(self, vendor_id, **kw):
        return call_result.DataTransfer(status="UnknownVendorId")
