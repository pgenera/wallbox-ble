"""Base entity for Wallbox BLE."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import WallboxBleCoordinator


class WallboxBleEntity(CoordinatorEntity[WallboxBleCoordinator]):
    """Shared base — supplies device_info and unique_id prefix."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: WallboxBleCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{coordinator.address}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
            manufacturer=MANUFACTURER,
            name=coordinator.entry.title,
            model=coordinator.state.layout_name or "Wallbox EVSE",
        )

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.state.connected
