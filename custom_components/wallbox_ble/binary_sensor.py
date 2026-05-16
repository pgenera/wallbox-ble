"""Binary sensors for Wallbox BLE."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import WallboxBleCoordinator, WallboxState
from .entity import WallboxBleEntity


@dataclass(frozen=True, kw_only=True)
class WBBinaryDesc(BinarySensorEntityDescription):
    value: Callable[[WallboxState], bool | None]


_DESCRIPTIONS: tuple[WBBinaryDesc, ...] = (
    WBBinaryDesc(
        key="car_connected",
        name="Car connected",
        device_class=BinarySensorDeviceClass.PLUG,
        value=lambda s: s.car_connected if s.status_code is not None else None,
    ),
    WBBinaryDesc(
        key="charging",
        name="Charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        value=lambda s: s.is_charging if s.status_code is not None else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: WallboxBleCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        WallboxBleBinarySensor(coordinator, desc) for desc in _DESCRIPTIONS
    )


class WallboxBleBinarySensor(WallboxBleEntity, BinarySensorEntity):
    entity_description: WBBinaryDesc

    def __init__(self, coordinator: WallboxBleCoordinator, description: WBBinaryDesc) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value(self.coordinator.state)
