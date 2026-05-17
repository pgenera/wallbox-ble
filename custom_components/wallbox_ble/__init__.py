"""Wallbox BLE custom component."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import WallboxBleCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.LOCK,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


CLIENTS_KEY = "_clients"  # shared across reloads, keyed by BLE address


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Wallbox BLE config entry."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    clients: dict = domain_data.setdefault(CLIENTS_KEY, {})

    coordinator = WallboxBleCoordinator(hass, entry)
    # Reuse an existing BLE client if we still hold one from a previous load —
    # HA reloads us on every entity enable/disable, and reconnecting over a
    # proxy is slow enough to make entities flap unavailable. The client
    # outlives the coordinator; we only disconnect in async_remove_entry.
    cached = clients.get(coordinator.address)
    if cached is not None and cached.is_connected:
        coordinator.client = cached

    await coordinator.async_config_entry_first_refresh()
    clients[coordinator.address] = coordinator.client

    domain_data[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: WallboxBleCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        # Drop our coordinator reference but keep the BLE client alive in the
        # shared cache so a reload (e.g. after enabling a disabled entity)
        # doesn't reconnect. Removal happens in async_remove_entry.
        coordinator.detach_client()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Called when the config entry is removed permanently."""
    clients: dict = hass.data.get(DOMAIN, {}).get(CLIENTS_KEY, {})
    address = entry.unique_id or entry.data.get("address")
    client = clients.pop(address, None) if address else None
    if client is not None:
        await client.disconnect()


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    # Options (PIN, UUID overrides) genuinely require a reconnect — drop the
    # cached client so the reload below sees a fresh connection.
    clients: dict = hass.data.get(DOMAIN, {}).get(CLIENTS_KEY, {})
    address = entry.unique_id or entry.data.get("address")
    client = clients.pop(address, None) if address else None
    if client is not None:
        await client.disconnect()
    await hass.config_entries.async_reload(entry.entry_id)
