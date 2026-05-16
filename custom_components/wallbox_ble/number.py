"""Number entities for Wallbox BLE."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfElectricCurrent, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import WallboxBleCoordinator
from .entity import WallboxBleEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: WallboxBleCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            MaxChargingCurrent(coordinator),
            EcoSmartPercent(coordinator),
            HaloBrightness(coordinator),
            HaloTimeSeconds(coordinator),
            AutoLockTime(coordinator),
        ]
    )


class MaxChargingCurrent(WallboxBleEntity, NumberEntity):
    _attr_name = "Max charging current"
    _attr_device_class = NumberDeviceClass.CURRENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_native_min_value = 6
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "max_current")

    @property
    def native_max_value(self) -> float:
        return float(self.coordinator.state.max_available_current or 32)

    @property
    def native_value(self) -> float | None:
        return self.coordinator.state.set_current

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_send("w_mxI", int(value))


class EcoSmartPercent(WallboxBleEntity, NumberEntity):
    _attr_name = "Eco Smart percentage"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "eco_percent_number")

    @property
    def native_value(self) -> float | None:
        return self.coordinator.state.eco_percent

    async def async_set_native_value(self, value: float) -> None:
        s = self.coordinator.state
        mode = s.eco_mode if s.eco_mode is not None else 0
        await self.coordinator.async_send(
            "s_ecos", {"esm": mode, "ese": 1 if mode else 0, "esp": int(value)}
        )


class HaloBrightness(WallboxBleEntity, NumberEntity):
    _attr_name = "Halo LED brightness"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "halo_brightness")

    @property
    def native_value(self) -> float | None:
        return self.coordinator.state.halo_brightness

    async def async_set_native_value(self, value: float) -> None:
        s = self.coordinator.state
        await self.coordinator.async_send(
            "s_halocfg",
            {
                "bright": int(value),
                "mode": s.halo_mode if s.halo_mode is not None else 1,
                "time_s": s.halo_time_s if s.halo_time_s is not None else 10,
            },
        )


class HaloTimeSeconds(WallboxBleEntity, NumberEntity):
    _attr_name = "Halo LED timeout"
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_native_min_value = 0
    _attr_native_max_value = 3600
    _attr_native_step = 1
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "halo_time_s")

    @property
    def native_value(self) -> float | None:
        return self.coordinator.state.halo_time_s

    async def async_set_native_value(self, value: float) -> None:
        s = self.coordinator.state
        await self.coordinator.async_send(
            "s_halocfg",
            {
                "bright": s.halo_brightness if s.halo_brightness is not None else 100,
                "mode": s.halo_mode if s.halo_mode is not None else 1,
                "time_s": int(value),
            },
        )


class AutoLockTime(WallboxBleEntity, NumberEntity):
    _attr_name = "Auto-lock timeout"
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_native_min_value = 10
    _attr_native_max_value = 600
    _attr_native_step = 5
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "auto_lock_time")

    @property
    def native_value(self) -> float | None:
        return self.coordinator.state.auto_lock_time_s

    async def async_set_native_value(self, value: float) -> None:
        s = self.coordinator.state
        await self.coordinator.async_send(
            "s_alo",
            {"enabled": 1 if s.auto_lock_enabled else 0, "time": int(value)},
        )
