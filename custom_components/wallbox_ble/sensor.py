"""Sensors for Wallbox BLE."""

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
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .bapi import ocpp_status_name
from .const import DOMAIN
from .coordinator import WallboxBleCoordinator, WallboxState
from .entity import WallboxBleEntity


@dataclass(frozen=True, kw_only=True)
class WBSensorDesc(SensorEntityDescription):
    value: Callable[[WallboxState], Any]


_DESCRIPTIONS: tuple[WBSensorDesc, ...] = (
    WBSensorDesc(
        key="status",
        name="Status",
        value=lambda s: s.status,
    ),
    WBSensorDesc(
        key="status_code",
        name="Status code",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value=lambda s: s.status_code,
    ),
    WBSensorDesc(
        key="charging_power",
        name="Charging power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=2,
        value=lambda s: s.charging_power_kw,
    ),
    WBSensorDesc(
        key="current_l1",
        name="Current L1",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=1,
        value=lambda s: s.current_l1,
    ),
    WBSensorDesc(
        key="current_l2",
        name="Current L2",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=1,
        value=lambda s: s.current_l2,
    ),
    WBSensorDesc(
        key="current_l3",
        name="Current L3",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=1,
        value=lambda s: s.current_l3,
    ),
    WBSensorDesc(
        key="voltage_l1",
        name="Voltage L1",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        entity_registry_enabled_default=False,
        value=lambda s: s.voltage_l1,
    ),
    WBSensorDesc(
        key="voltage_l2",
        name="Voltage L2",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        entity_registry_enabled_default=False,
        value=lambda s: s.voltage_l2,
    ),
    WBSensorDesc(
        key="voltage_l3",
        name="Voltage L3",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        entity_registry_enabled_default=False,
        value=lambda s: s.voltage_l3,
    ),
    WBSensorDesc(
        key="power_l1",
        name="Power L1",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        entity_registry_enabled_default=False,
        value=lambda s: s.power_l1,
    ),
    WBSensorDesc(
        key="power_l2",
        name="Power L2",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        entity_registry_enabled_default=False,
        value=lambda s: s.power_l2,
    ),
    WBSensorDesc(
        key="power_l3",
        name="Power L3",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        entity_registry_enabled_default=False,
        value=lambda s: s.power_l3,
    ),
    WBSensorDesc(
        key="house_current_l1",
        name="House current L1",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        entity_registry_enabled_default=False,
        value=lambda s: s.house_current_l1,
    ),
    WBSensorDesc(
        key="house_current_l2",
        name="House current L2",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        entity_registry_enabled_default=False,
        value=lambda s: s.house_current_l2,
    ),
    WBSensorDesc(
        key="house_current_l3",
        name="House current L3",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        entity_registry_enabled_default=False,
        value=lambda s: s.house_current_l3,
    ),
    WBSensorDesc(
        key="session_energy",
        name="Session energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value=lambda s: s.session_energy_kwh,
    ),
    WBSensorDesc(
        key="session_green_energy",
        name="Session green energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        entity_registry_enabled_default=False,
        value=lambda s: s.session_green_kwh,
    ),
    WBSensorDesc(
        key="session_grid_energy",
        name="Session grid energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        entity_registry_enabled_default=False,
        value=lambda s: s.session_grid_kwh,
    ),
    WBSensorDesc(
        key="session_discharge_energy",
        name="Session discharge energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        entity_registry_enabled_default=False,
        value=lambda s: s.session_discharge_kwh,
    ),
    WBSensorDesc(
        key="lifetime_energy",
        name="Lifetime energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value=lambda s: s.lifetime_energy_kwh,
    ),
    WBSensorDesc(
        key="max_available_current",
        name="Max available current",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda s: s.max_available_current,
    ),
    WBSensorDesc(
        key="phases_connection",
        name="Phases",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value=lambda s: s.phases_connection,
    ),
    WBSensorDesc(
        key="ocpp_status",
        name="OCPP status",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value=lambda s: ocpp_status_name(s.ocpp_status, s.layout_name),
    ),
    WBSensorDesc(
        key="ocpp_status_code",
        name="OCPP status code",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value=lambda s: s.ocpp_status,
    ),
    WBSensorDesc(
        key="eco_percent",
        name="Eco Smart percentage",
        native_unit_of_measurement=PERCENTAGE,
        entity_registry_enabled_default=False,
        value=lambda s: s.eco_percent,
    ),
    WBSensorDesc(
        key="timezone",
        name="Charger timezone",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value=lambda s: s.timezone,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: WallboxBleCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        WallboxBleSensor(coordinator, desc) for desc in _DESCRIPTIONS
    )


class WallboxBleSensor(WallboxBleEntity, SensorEntity):
    entity_description: WBSensorDesc

    def __init__(self, coordinator: WallboxBleCoordinator, description: WBSensorDesc) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value(self.coordinator.state)
