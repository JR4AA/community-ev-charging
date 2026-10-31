"""A pretend charger, for testing the whole flow without the real one.

    python -m evcharge.simulator ws://127.0.0.1:9000/CP1 --password … --kw 7 --speed 60 [--ocpp 2.0.1]

It speaks OCPP 1.6J, or 2.0.1 with --ocpp 2.0.1. It boots, reports a car plugged in (Preparing), accepts the remote start that
payment sends, delivers energy at --kw (time sped up by --speed), and stops on
a remote stop, or by "unplugging" after --unplug-after-kwh.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
from datetime import datetime, timedelta, timezone

import websockets
from ocpp.routing import on
from ocpp.v16 import ChargePoint as OcppChargePoint
from ocpp.v16 import call, call_result
from ocpp.v201 import ChargePoint as OcppChargePoint201
from ocpp.v201 import call as call201
from ocpp.v201 import call_result as call_result201
from ocpp.v201.enums import Action as Action201
from ocpp.v16.enums import (
    Action,
    ChargePointErrorCode,
    ChargePointStatus,
    ConfigurationStatus,
    DataTransferStatus,
    RegistrationStatus,
    RemoteStartStopStatus,
    TriggerMessageStatus,
)

log = logging.getLogger("simulator")


class SimulatedCharger(OcppChargePoint):
    def __init__(self, cp_id, connection, connector=1, kw=7.0, tick=1.0, speed=60.0, unplug_after_kwh=None,
                 screen=True):
        super().__init__(cp_id, connection)
        self.connector = connector
        self.kw = kw
        self.tick = tick
        self.speed = speed
        self.unplug_after_wh = None if unplug_after_kwh is None else int(unplug_after_kwh * 1000)
        self.meter_wh = 100_000              # lifetime register, not zero
        self.clock = datetime.now(timezone.utc)
        self.transaction_id: int | None = None
        self.stop_requested = asyncio.Event()
        self.finished = asyncio.Event()
        self.config: dict[str, str] = {"MeterValueSampleInterval": "300", "NetworkType": "WiFi",
                                       "WifiSignalStrength": "-61"}
        if screen:   # a charger with the OCA price-display extension
            self.config |= {"CustomDisplayCostAndPrice": "false", "DefaultPrice": ""}
        self.screen: list[tuple[str, dict]] = []   # price messages received
        self._charging: asyncio.Task | None = None

    def now(self) -> str:
        return self.clock.isoformat()

    @property
    def sample_interval(self) -> str:
        return self.config["MeterValueSampleInterval"]

    async def status(self, status: ChargePointStatus) -> None:
        await self.call(call.StatusNotification(
            connector_id=self.connector, error_code=ChargePointErrorCode.no_error, status=status, timestamp=self.now()))

    async def boot_and_plug_in(self) -> None:
        r = await self.call(call.BootNotification(charge_point_model="Simulator", charge_point_vendor="evcharge"))
        assert r.status == RegistrationStatus.accepted
        await self.status(ChargePointStatus.available)
        await self.status(ChargePointStatus.preparing)    # car plugged in

    @on(Action.change_configuration)
    def on_change_configuration(self, key, value):
        self.config[key] = value
        return call_result.ChangeConfiguration(status=ConfigurationStatus.accepted)

    @on(Action.get_configuration)
    def on_get_configuration(self, key=None):
        wanted = key or list(self.config)
        return call_result.GetConfiguration(
            configuration_key=[{"key": k, "readonly": k in ("NetworkType", "WifiSignalStrength"), "value": self.config[k]}
                               for k in wanted if k in self.config],
            unknown_key=[k for k in wanted if k not in self.config] or None)

    @on(Action.data_transfer)
    def on_data_transfer(self, vendor_id, message_id=None, data=None):
        if vendor_id != "org.openchargealliance.costmsg" or "CustomDisplayCostAndPrice" not in self.config:
            return call_result.DataTransfer(status=DataTransferStatus.unknown_vendor_id)
        self.screen.append((message_id, json.loads(data)))
        return call_result.DataTransfer(status=DataTransferStatus.accepted)

    @on(Action.trigger_message)
    def on_trigger(self, requested_message, connector_id=None):
        return call_result.TriggerMessage(status=TriggerMessageStatus.accepted)

    @on(Action.remote_start_transaction)
    def on_remote_start(self, id_tag, connector_id=None, **kw):
        if self._charging:
            return call_result.RemoteStartTransaction(status=RemoteStartStopStatus.rejected)
        self._charging = asyncio.create_task(self.charge(id_tag))
        return call_result.RemoteStartTransaction(status=RemoteStartStopStatus.accepted)

    @on(Action.remote_stop_transaction)
    def on_remote_stop(self, transaction_id):
        if transaction_id != self.transaction_id:
            return call_result.RemoteStopTransaction(status=RemoteStartStopStatus.rejected)
        self.stop_requested.set()
        return call_result.RemoteStopTransaction(status=RemoteStartStopStatus.accepted)

    async def charge(self, id_tag: str) -> None:
        await asyncio.sleep(0.05)   # let the remote start reply go first
        r = await self.call(call.StartTransaction(
            connector_id=self.connector, id_tag=id_tag, meter_start=self.meter_wh, timestamp=self.now()))
        self.transaction_id = r.transaction_id
        if r.id_tag_info["status"] != "Accepted":
            log.warning("start refused: %s", r.id_tag_info)
            await self.stop("DeAuthorized")
            return
        await self.status(ChargePointStatus.charging)
        start = self.meter_wh
        reason = "Remote"
        while not self.stop_requested.is_set():
            await asyncio.sleep(self.tick)
            step = timedelta(seconds=self.tick * self.speed)
            self.clock += step
            self.meter_wh += int(self.kw * 1000 * step.total_seconds() / 3600)
            await self.call(call.MeterValues(connector_id=self.connector, transaction_id=self.transaction_id,
                                             meter_value=[{"timestamp": self.now(), "sampledValue": [
                                                 {"value": f"{self.meter_wh / 1000:.3f}", "unit": "kWh",
                                                  "measurand": "Energy.Active.Import.Register"}]}]))
            log.info("delivered %.3f kWh", (self.meter_wh - start) / 1000)
            if self.unplug_after_wh is not None and self.meter_wh - start >= self.unplug_after_wh:
                reason = "EVDisconnected"
                break
        await self.stop(reason)

    async def stop(self, reason: str) -> None:
        await self.call(call.StopTransaction(meter_stop=self.meter_wh, timestamp=self.now(),
                                             transaction_id=self.transaction_id, reason=reason))
        await self.status(ChargePointStatus.finishing)
        await self.status(ChargePointStatus.available)
        self.finished.set()


class SimulatedCharger201(OcppChargePoint201):
    """The same pretend charger speaking OCPP 2.0.1. With tx_on_plug_in it starts a
    transaction when the cable goes in, before payment, as some 2.0.1 chargers do."""

    def __init__(self, cp_id, connection, connector=1, kw=7.0, tick=1.0, speed=60.0, unplug_after_kwh=None,
                 screen=True, tx_on_plug_in=False):
        super().__init__(cp_id, connection)
        self.connector = connector
        self.kw = kw
        self.tick = tick
        self.speed = speed
        self.unplug_after_wh = None if unplug_after_kwh is None else int(unplug_after_kwh * 1000)
        self.tx_on_plug_in = tx_on_plug_in
        self.meter_wh = 100_000
        self.clock = datetime.now(timezone.utc)
        self.transaction_id: str | None = None
        self.seq = 0
        self.stop_requested = asyncio.Event()
        self.finished = asyncio.Event()
        # "Component.Variable" or "Component.Variable[instance]" -> value
        self.config: dict[str, str] = {"SampledDataCtrlr.TxUpdatedInterval": "300",
                                       "OCPPCommCtrlr.NetworkConfigurationPriority": "1",
                                       "ChargingStation.Model": "Simulator"}
        if screen:   # a charger with the standard tariff and cost display
            self.config |= {"TariffCostCtrlr.Enabled[Tariff]": "false", "TariffCostCtrlr.Enabled[Cost]": "false",
                            "TariffCostCtrlr.TariffFallbackMessage": "", "TariffCostCtrlr.Currency": "EUR"}
        self.screen: list[tuple[str, dict]] = []   # (event type, cost and message) from TransactionEvent responses
        self._charging: asyncio.Task | None = None

    def now(self) -> str:
        return self.clock.isoformat()

    @property
    def sample_interval(self) -> str:
        return self.config["SampledDataCtrlr.TxUpdatedInterval"]

    def meter(self, context: str) -> list[dict]:
        return [{"timestamp": self.now(), "sampledValue": [
            {"value": self.meter_wh / 1000, "measurand": "Energy.Active.Import.Register", "context": context,
             "unitOfMeasure": {"unit": "kWh"}}]}]

    async def status(self, status: str) -> None:
        await self.call(call201.StatusNotification(timestamp=self.now(), connector_status=status,
                                                   evse_id=self.connector, connector_id=1))

    async def event(self, event_type: str, trigger: str, state: str | None = None, context="Sample.Periodic",
                    id_tag: str | None = None, remote_start_id: int | None = None, reason: str | None = None):
        self.seq += 1
        info = {"transaction_id": self.transaction_id}
        if state:
            info["charging_state"] = state
        if remote_start_id is not None:
            info["remote_start_id"] = remote_start_id
        if reason:
            info["stopped_reason"] = reason
        r = await self.call(call201.TransactionEvent(
            event_type=event_type, timestamp=self.now(), trigger_reason=trigger, seq_no=self.seq,
            transaction_info=info, meter_value=self.meter(context),
            evse={"id": self.connector, "connector_id": 1} if event_type == "Started" else None,
            id_token={"id_token": id_tag, "type": "Central"} if id_tag else None))
        if r.total_cost is not None:
            self.screen.append((event_type, {"cost": r.total_cost, **(r.updated_personal_message or {})}))
        return r

    async def boot_and_plug_in(self) -> None:
        r = await self.call(call201.BootNotification(charging_station={"model": "Simulator", "vendor_name": "evcharge"},
                                                     reason="PowerUp"))
        assert r.status == "Accepted"
        await self.status("Available")
        await self.status("Occupied")                     # car plugged in
        if self.tx_on_plug_in:
            self.transaction_id = f"sim-{self.seq}-{self.meter_wh}"
            await self.event("Started", "CablePluggedIn", "EVConnected", "Transaction.Begin")

    @on(Action201.set_variables)
    def on_set_variables(self, set_variable_data):
        results = []
        for d in set_variable_data:
            key = f"{d['component']['name']}.{d['variable']['name']}" + (
                f"[{d['variable']['instance']}]" if d["variable"].get("instance") else "")
            known = key in self.config or not key.startswith("TariffCostCtrlr")
            if known:
                self.config[key] = d["attribute_value"]
            results.append({"attribute_status": "Accepted" if known else "UnknownComponent",
                            "component": d["component"], "variable": d["variable"]})
        return call_result201.SetVariables(set_variable_result=results)

    @on(Action201.get_base_report)
    def on_get_base_report(self, request_id, report_base):
        asyncio.create_task(self.report(request_id))
        return call_result201.GetBaseReport(status="Accepted")

    async def report(self, request_id: int) -> None:
        """The inventory, in two NotifyReport messages."""
        await asyncio.sleep(0.05)
        data = []
        for key, value in self.config.items():
            component, _, variable = key.partition(".")
            name, _, instance = variable.rstrip("]").partition("[")
            data.append({"component": {"name": component},
                         "variable": {"name": name, **({"instance": instance} if instance else {})},
                         "variableAttribute": [{"value": value,
                                                "mutability": "ReadOnly" if component == "ChargingStation" else "ReadWrite"}]})
        half = len(data) // 2
        for seq, (part, tbc) in enumerate(((data[:half], True), (data[half:], False))):
            await self.call(call201.NotifyReport(request_id=request_id, generated_at=self.now(), seq_no=seq,
                                                 report_data=part, tbc=tbc))

    @on(Action201.trigger_message)
    def on_trigger(self, requested_message, evse=None):
        return call_result201.TriggerMessage(status="Accepted")

    @on(Action201.request_start_transaction)
    def on_request_start(self, id_token, remote_start_id, evse_id=None, **kw):
        if self._charging:
            return call_result201.RequestStartTransaction(status="Rejected")
        self._charging = asyncio.create_task(self.charge(id_token["id_token"], remote_start_id))
        return call_result201.RequestStartTransaction(status="Accepted", transaction_id=self.transaction_id)

    @on(Action201.request_stop_transaction)
    def on_request_stop(self, transaction_id):
        if transaction_id != self.transaction_id:
            return call_result201.RequestStopTransaction(status="Rejected")
        self.stop_requested.set()
        return call_result201.RequestStopTransaction(status="Accepted")

    async def charge(self, id_tag: str, remote_start_id: int) -> None:
        await asyncio.sleep(0.05)   # let the request's reply go first
        if self.transaction_id:     # already started when the cable went in
            r = await self.event("Updated", "RemoteStart", "Charging", id_tag=id_tag, remote_start_id=remote_start_id)
        else:
            self.transaction_id = f"sim-{self.seq}-{self.meter_wh}"
            r = await self.event("Started", "RemoteStart", "Charging", "Transaction.Begin", id_tag, remote_start_id)
        if r.id_token_info["status"] != "Accepted":
            log.warning("start refused: %s", r.id_token_info)
            await self.stop("DeAuthorized", "Deauthorized")
            return
        start = self.meter_wh
        reason, trigger = "Remote", "RemoteStop"
        while not self.stop_requested.is_set():
            await asyncio.sleep(self.tick)
            step = timedelta(seconds=self.tick * self.speed)
            self.clock += step
            self.meter_wh += int(self.kw * 1000 * step.total_seconds() / 3600)
            await self.event("Updated", "MeterValuePeriodic", "Charging")
            log.info("delivered %.3f kWh", (self.meter_wh - start) / 1000)
            if self.unplug_after_wh is not None and self.meter_wh - start >= self.unplug_after_wh:
                reason, trigger = "EVDisconnected", "EVCommunicationLost"
                break
        await self.stop(reason, trigger)

    async def stop(self, reason: str, trigger: str) -> None:
        await self.event("Ended", trigger, "Idle", "Transaction.End", reason=reason)
        await self.status("Available")
        self.finished.set()


CHARGERS = {"1.6": (SimulatedCharger, "ocpp1.6"), "2.0.1": (SimulatedCharger201, "ocpp2.0.1")}


def connect(url: str, cp_id: str, password: str | None = None, version: str = "1.6"):
    headers = {}
    if password:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{cp_id}:{password}".encode()).decode()
    return websockets.connect(url, subprotocols=[CHARGERS[version][1]], additional_headers=headers)


async def main() -> None:
    p = argparse.ArgumentParser(prog="evcharge.simulator")
    p.add_argument("url", help="e.g. ws://127.0.0.1:9000/CP1 (the last path part is the charge point id)")
    p.add_argument("--password")
    p.add_argument("--connector", type=int, default=1)
    p.add_argument("--kw", type=float, default=7.0)
    p.add_argument("--tick", type=float, default=2.0, help="real seconds between meter readings")
    p.add_argument("--speed", type=float, default=60.0, help="simulated seconds per real second")
    p.add_argument("--unplug-after-kwh", type=float)
    p.add_argument("--ocpp", choices=list(CHARGERS), default="1.6")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cp_id = a.url.rstrip("/").rsplit("/", 1)[-1]
    async with connect(a.url, cp_id, a.password, a.ocpp) as ws:
        cp = CHARGERS[a.ocpp][0](cp_id, ws, a.connector, a.kw, a.tick, a.speed, a.unplug_after_kwh)
        runner = asyncio.create_task(cp.start())
        await cp.boot_and_plug_in()
        print(f"{cp_id}: car plugged in. Pay on the pay page; Ctrl+C to quit.")
        await cp.finished.wait()
        await asyncio.sleep(1)
        runner.cancel()


if __name__ == "__main__":
    asyncio.run(main())
