"""Sensors: socket state, power and live session; site totals."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import SIGNAL_STRENGTH_DECIBELS_MILLIWATT, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ChargingConfigEntry
from .const import SOCKET_STATES
from .entity import ChargingEntity


@dataclass(frozen=True, kw_only=True)
class ChargingSensorDescription(SensorEntityDescription):
    value: Callable[[ChargingEntity], Any]
    currency: bool = False


def _state(e: ChargingEntity) -> str:
    state = str(e.socket_data.get("state") or "unknown").lower()
    return state if state in SOCKET_STATES else "unknown"


def _session(field: str) -> Callable[[ChargingEntity], Any]:
    return lambda e: (e.session or {}).get(field, 0) or 0


SOCKET_SENSORS = (
    ChargingSensorDescription(key="state", device_class=SensorDeviceClass.ENUM, options=SOCKET_STATES, value=_state),
    ChargingSensorDescription(key="power", device_class=SensorDeviceClass.POWER, native_unit_of_measurement=UnitOfPower.KILO_WATT,
                              state_class=SensorStateClass.MEASUREMENT, value=lambda e: e.socket_data.get("power_kw", 0)),
    ChargingSensorDescription(key="session_energy", device_class=SensorDeviceClass.ENERGY,
                              native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, state_class=SensorStateClass.TOTAL,
                              suggested_display_precision=2, value=_session("energy_kwh")),
    ChargingSensorDescription(key="session_cost", device_class=SensorDeviceClass.MONETARY, currency=True,
                              suggested_display_precision=2, value=_session("cost")),
)


def _signal(radio: str) -> Callable[[ChargingEntity], Any]:
    """The charger's strength for one radio, from its own reported values."""
    def value(e: ChargingEntity) -> Any:
        cp = e.socket_data.get("charge_point")
        charger = (e.coordinator.data.get("chargers") or {}).get(cp) or {}
        return (charger.get("signal") or {}).get(radio)
    return value


def signal_sensor(radio: str) -> ChargingSensorDescription:
    return ChargingSensorDescription(
        key=f"signal_{radio}",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        value=_signal(radio),
    )


def _site(section: str, field: str) -> Callable[[ChargingEntity], Any]:
    return lambda e: (e.coordinator.data.get(section) or {}).get(field, 0)


SITE_SENSORS = (
    ChargingSensorDescription(key="today_energy", device_class=SensorDeviceClass.ENERGY,
                              native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, state_class=SensorStateClass.TOTAL,
                              suggested_display_precision=2, value=_site("today", "kwh")),
    ChargingSensorDescription(key="month_energy", device_class=SensorDeviceClass.ENERGY,
                              native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, state_class=SensorStateClass.TOTAL,
                              suggested_display_precision=2, value=_site("month", "kwh")),
    ChargingSensorDescription(key="month_taken", device_class=SensorDeviceClass.MONETARY, currency=True,
                              suggested_display_precision=2, value=_site("month", "taken")),
    ChargingSensorDescription(key="month_fees", device_class=SensorDeviceClass.MONETARY, currency=True,
                              suggested_display_precision=2, value=_site("month", "paypal_fees")),
    ChargingSensorDescription(key="month_charges", state_class=SensorStateClass.TOTAL, value=_site("month", "charges")),
    # Days until the certificate the charger meets expires. None (so "unknown")
    # when the check is off or the endpoint can't be reached - the binary sensor
    # carries that distinction.
    ChargingSensorDescription(key="certificate_days", native_unit_of_measurement="d",
                              value=lambda e: (e.coordinator.data.get("tls") or {}).get("days_left")),
)


async def async_setup_entry(hass: HomeAssistant, entry: ChargingConfigEntry, add: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    entities: list[ChargingSensor] = [ChargingSensor(coordinator, d, None) for d in SITE_SENSORS]
    for socket in coordinator.data.get("sockets", {}):
        entities += [ChargingSensor(coordinator, d, socket) for d in SOCKET_SENSORS]
    # Radio strength belongs to the charge point, not the socket, so put it on
    # the first socket of each one - that device already carries the charger's
    # vendor, model and serial. Which radios exist is the charger's choice.
    seen: set[str] = set()
    for socket, data in (coordinator.data.get("sockets") or {}).items():
        cp = data.get("charge_point")
        if cp in seen:
            continue
        seen.add(cp)
        signal = ((coordinator.data.get("chargers") or {}).get(cp) or {}).get("signal") or {}
        entities += [ChargingSensor(coordinator, signal_sensor(r), socket) for r in sorted(signal)]
    add(entities)


class ChargingSensor(ChargingEntity, SensorEntity):
    entity_description: ChargingSensorDescription

    def __init__(self, coordinator, description: ChargingSensorDescription, socket: str | None) -> None:
        super().__init__(coordinator, description.key, socket)
        self.entity_description = description
        if description.currency:
            self._attr_native_unit_of_measurement = coordinator.data.get("currency", "GBP")

    @property
    def native_value(self) -> Any:
        return self.entity_description.value(self)
