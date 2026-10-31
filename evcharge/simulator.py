"""A pretend charger, for testing the whole flow without the real one.

    python -m evcharge.simulator ws://127.0.0.1:9000/CP1 --password … --kw 7 --speed 60

It boots, reports a car plugged in (Preparing), accepts the remote start that
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


def connect(url: str, cp_id: str, password: str | None = None):
    headers = {}
    if password:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{cp_id}:{password}".encode()).decode()
    return websockets.connect(url, subprotocols=["ocpp1.6"], additional_headers=headers)


async def main() -> None:
    p = argparse.ArgumentParser(prog="evcharge.simulator")
    p.add_argument("url", help="e.g. ws://127.0.0.1:9000/CP1 (the last path part is the charge point id)")
    p.add_argument("--password")
    p.add_argument("--connector", type=int, default=1)
    p.add_argument("--kw", type=float, default=7.0)
    p.add_argument("--tick", type=float, default=2.0, help="real seconds between meter readings")
    p.add_argument("--speed", type=float, default=60.0, help="simulated seconds per real second")
    p.add_argument("--unplug-after-kwh", type=float)
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cp_id = a.url.rstrip("/").rsplit("/", 1)[-1]
    async with connect(a.url, cp_id, a.password) as ws:
        cp = SimulatedCharger(cp_id, ws, a.connector, a.kw, a.tick, a.speed, a.unplug_after_kwh)
        runner = asyncio.create_task(cp.start())
        await cp.boot_and_plug_in()
        print(f"{cp_id}: car plugged in. Pay on the pay page; Ctrl+C to quit.")
        await cp.finished.wait()
        await asyncio.sleep(1)
        runner.cancel()


if __name__ == "__main__":
    asyncio.run(main())
