"""Shared entity base: one device per socket, and one for the site."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ChargingCoordinator


class ChargingEntity(CoordinatorEntity[ChargingCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: ChargingCoordinator, key: str, socket: str | None) -> None:
        super().__init__(coordinator)
        entry_id = coordinator.config_entry.entry_id
        self.socket = socket
        self._attr_unique_id = f"{entry_id}_{socket or 'site'}_{key}"
        self._attr_translation_key = key
        site = (DOMAIN, f"{entry_id}_site")
        if socket is None:
            self._attr_device_info = DeviceInfo(identifiers={site}, name="EV charging", manufacturer="community-ev-charging")
        else:
            cp = self.socket_data.get("charge_point", socket)
            info = (coordinator.data.get("chargers") or {}).get(cp, {})
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, f"{entry_id}_{socket}")}, name=f"Socket {socket}",
                manufacturer=info.get("vendor"), model=info.get("model"), sw_version=info.get("firmware"),
                serial_number=info.get("serial"))

    @property
    def socket_data(self) -> dict:
        return (self.coordinator.data.get("sockets") or {}).get(self.socket, {})

    @property
    def session(self) -> dict | None:
        return self.socket_data.get("session")
