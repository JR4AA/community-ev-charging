"""Binary sensors: socket online and charging."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ChargingConfigEntry
from .entity import ChargingEntity

CHARGING_STATES = {"charging", "suspendedev", "suspendedevse"}


async def async_setup_entry(hass: HomeAssistant, entry: ChargingConfigEntry, add: AddEntitiesCallback) -> None:
    c = entry.runtime_data
    entities: list[BinarySensorEntity] = []
    for socket in c.data.get("sockets", {}):
        entities += [Online(c, socket), Charging(c, socket)]
    add(entities)


class Online(ChargingEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator, socket: str) -> None:
        super().__init__(coordinator, "online", socket)

    @property
    def is_on(self) -> bool:
        return bool(self.socket_data.get("online"))


class Charging(ChargingEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    def __init__(self, coordinator, socket: str) -> None:
        super().__init__(coordinator, "charging", socket)

    @property
    def is_on(self) -> bool:
        return str(self.socket_data.get("state", "")).lower() in CHARGING_STATES

    @property
    def extra_state_attributes(self) -> dict | None:
        s = self.session
        return {k: s.get(k) for k in ("kind", "started_at", "hold", "limit_kwh", "note")} if s else None
