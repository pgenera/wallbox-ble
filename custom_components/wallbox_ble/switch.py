"""Switches for Wallbox BLE."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .bapi import W_CHA_START, W_CHA_STOP
from .const import DOMAIN
from .coordinator import WallboxBleCoordinator
from .entity import WallboxBleEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: WallboxBleCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            ChargeSwitch(coordinator),
            EcoSmartSwitch(coordinator),
            PhaseSwitch(coordinator),
            PowerSharingDynamicSwitch(coordinator),
            AutoLockSwitch(coordinator),
            BleInUseSwitch(coordinator),
        ]
    )


class ChargeSwitch(WallboxBleEntity, SwitchEntity):
    _attr_name = "Charge"
    _attr_icon = "mdi:ev-station"

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "charge")

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.state.is_charging if self.coordinator.state.status_code is not None else None

    async def async_turn_on(self, **_: Any) -> None:
        await self.coordinator.async_send("w_cha", W_CHA_START)

    async def async_turn_off(self, **_: Any) -> None:
        await self.coordinator.async_send("w_cha", W_CHA_STOP)


class EcoSmartSwitch(WallboxBleEntity, SwitchEntity):
    _attr_name = "Eco Smart"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "eco_enabled")

    @property
    def is_on(self) -> bool | None:
        s = self.coordinator.state
        if s.eco_mode is None:
            return None
        return s.eco_mode != 0

    async def _set(self, enabled: bool) -> None:
        s = self.coordinator.state
        mode = s.eco_mode if (s.eco_mode and s.eco_mode != 0) else 1
        target_mode = mode if enabled else 0
        await self.coordinator.async_send(
            "s_ecos",
            {"esm": target_mode, "ese": 1 if enabled else 0, "esp": s.eco_percent or 100},
        )

    async def async_turn_on(self, **_: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **_: Any) -> None:
        await self._set(False)


class PhaseSwitch(WallboxBleEntity, SwitchEntity):
    _attr_name = "Phase switching"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "phase_switch")

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.state.phase_switch_enabled

    async def async_turn_on(self, **_: Any) -> None:
        await self.coordinator.async_send("s_phsw", {"enabled": 1})

    async def async_turn_off(self, **_: Any) -> None:
        await self.coordinator.async_send("s_phsw", {"enabled": 0})


class PowerSharingDynamicSwitch(WallboxBleEntity, SwitchEntity):
    _attr_name = "Dynamic power sharing"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "power_sharing_dynamic")

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.state.power_sharing_dynamic

    async def async_turn_on(self, **_: Any) -> None:
        await self.coordinator.async_send("s_psh", {"dyps": 1})

    async def async_turn_off(self, **_: Any) -> None:
        await self.coordinator.async_send("s_psh", {"dyps": 0})


class BleInUseSwitch(WallboxBleEntity, SwitchEntity):
    """Release the BLE link so the Wallbox phone app can connect.

    A Wallbox accepts only one BLE central at a time. Turn this off to let
    the app talk to the charger, turn it back on to resume polling.
    """

    _attr_name = "BLE in use"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:bluetooth-connect"

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "ble_in_use")

    @property
    def is_on(self) -> bool:
        return not self.coordinator.released

    @property
    def available(self) -> bool:
        # Always available — it's how the user re-acquires the link.
        return True

    async def async_turn_on(self, **_: Any) -> None:
        await self.coordinator.async_acquire()

    async def async_turn_off(self, **_: Any) -> None:
        await self.coordinator.async_release()


class AutoLockSwitch(WallboxBleEntity, SwitchEntity):
    _attr_name = "Auto-lock"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: WallboxBleCoordinator) -> None:
        super().__init__(coordinator, "auto_lock_enabled")

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.state.auto_lock_enabled

    async def async_turn_on(self, **_: Any) -> None:
        t = self.coordinator.state.auto_lock_time_s or 60
        await self.coordinator.async_send("s_alo", {"enabled": 1, "time": t})

    async def async_turn_off(self, **_: Any) -> None:
        t = self.coordinator.state.auto_lock_time_s or 60
        await self.coordinator.async_send("s_alo", {"enabled": 0, "time": t})
