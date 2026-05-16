"""Lock entity for Wallbox BLE."""

from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import WallboxBleCoordinator
from .entity import WallboxBleEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: WallboxBleCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([WallboxSocketLock(coordinator)])


class WallboxSocketLock(WallboxBleEntity, LockEntity):
    _attr_name = "Socket lock"

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "lock")

    @property
    def is_locked(self) -> bool | None:
        return self.coordinator.state.locked

    async def async_lock(self, **_: Any) -> None:
        await self.coordinator.async_send("w_lck", 1)

    async def async_unlock(self, **_: Any) -> None:
        await self.coordinator.async_send("w_lck", 0)
