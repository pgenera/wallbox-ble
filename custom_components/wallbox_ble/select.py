"""Select entities for Wallbox BLE."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .bapi import ECO_MODE_LABELS
from .const import DOMAIN
from .coordinator import WallboxBleCoordinator
from .entity import WallboxBleEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: WallboxBleCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            EcoModeSelect(coordinator),
            HaloModeSelect(coordinator),
        ]
    )


class EcoModeSelect(WallboxBleEntity, SelectEntity):
    _attr_name = "Eco Smart mode"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "eco_mode")
        self._attr_options = list(ECO_MODE_LABELS.values())
        self._reverse = {v: k for k, v in ECO_MODE_LABELS.items()}

    @property
    def current_option(self) -> str | None:
        m = self.coordinator.state.eco_mode
        return ECO_MODE_LABELS.get(m) if m is not None else None

    async def async_select_option(self, option: str) -> None:
        mode = self._reverse[option]
        s = self.coordinator.state
        await self.coordinator.async_send(
            "s_ecos",
            {"esm": mode, "ese": 1 if mode else 0, "esp": s.eco_percent or 100},
        )


_HALO_LABELS = {0: "Default", 1: "Energy-aware"}


class HaloModeSelect(WallboxBleEntity, SelectEntity):
    _attr_name = "Halo LED mode"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "halo_mode")
        self._attr_options = list(_HALO_LABELS.values())
        self._reverse = {v: k for k, v in _HALO_LABELS.items()}

    @property
    def current_option(self) -> str | None:
        m = self.coordinator.state.halo_mode
        return _HALO_LABELS.get(m) if m is not None else None

    async def async_select_option(self, option: str) -> None:
        s = self.coordinator.state
        await self.coordinator.async_send(
            "s_halocfg",
            {
                "bright": s.halo_brightness if s.halo_brightness is not None else 100,
                "mode": self._reverse[option],
                "time_s": s.halo_time_s if s.halo_time_s is not None else 10,
            },
        )
