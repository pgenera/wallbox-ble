"""Config + options flow for Wallbox BLE."""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS

from .const import (
    CONF_FAST_POLL,
    CONF_NOTIFY_CHAR_UUID,
    CONF_PIN,
    CONF_SERVICE_UUID,
    CONF_SLOW_POLL,
    CONF_WRITE_CHAR_UUID,
    DEFAULT_FAST_POLL,
    DEFAULT_SLOW_POLL,
    DOMAIN,
    NAME_PATTERNS,
)

_NAME_RE = re.compile("|".join(NAME_PATTERNS), re.IGNORECASE)


def _looks_like_wallbox(info: BluetoothServiceInfoBleak) -> bool:
    name = info.name or info.advertisement.local_name or ""
    return bool(_NAME_RE.search(name))


class WallboxBleConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._discovered: dict[str, str] = {}  # address -> name
        self._pending_address: str | None = None
        self._pending_name: str | None = None

    # -- bluetooth discovery -----------------------------------------------

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        if not _looks_like_wallbox(discovery_info):
            return self.async_abort(reason="not_supported")
        self._pending_address = discovery_info.address
        self._pending_name = discovery_info.name or discovery_info.address
        self.context["title_placeholders"] = {"name": self._pending_name}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._pending_address is not None
        if user_input is not None:
            return self._create_entry(
                self._pending_address,
                self._pending_name or self._pending_address,
                user_input.get(CONF_PIN, ""),
            )
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({vol.Optional(CONF_PIN, default=""): str}),
            description_placeholders={"name": self._pending_name or ""},
        )

    # -- manual flow -------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self._create_entry(
                address,
                self._discovered.get(address, address),
                user_input.get(CONF_PIN, ""),
            )

        configured = {e.unique_id for e in self._async_current_entries()}
        self._discovered = {}
        for info in async_discovered_service_info(self.hass):
            if info.address in configured:
                continue
            if _looks_like_wallbox(info):
                self._discovered[info.address] = info.name or info.address

        if not self._discovered:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {addr: f"{name} ({addr})" for addr, name in self._discovered.items()}
                    ),
                    vol.Optional(CONF_PIN, default=""): str,
                }
            ),
        )

    # -- reauth ------------------------------------------------------------

    async def async_step_reauth(self, _entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            entry = self._get_reauth_entry()
            new_options = {**entry.options, CONF_PIN: user_input.get(CONF_PIN, "")}
            self.hass.config_entries.async_update_entry(entry, options=new_options)
            await self.hass.config_entries.async_reload(entry.entry_id)
            return self.async_abort(reason="reauth_successful")
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Optional(CONF_PIN, default=""): str}),
        )

    # -- helpers -----------------------------------------------------------

    def _create_entry(self, address: str, name: str, pin: str) -> ConfigFlowResult:
        return self.async_create_entry(
            title=name,
            data={CONF_ADDRESS: address},
            options={CONF_PIN: pin} if pin else {},
        )

    @staticmethod
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return WallboxBleOptionsFlow(entry)


class WallboxBleOptionsFlow(OptionsFlow):
    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            # Empty UUID fields become absent so auto-probe is restored.
            cleaned = {k: v for k, v in user_input.items() if v not in ("", None)}
            return self.async_create_entry(title="", data=cleaned)

        opts = self._entry.options
        schema = vol.Schema(
            {
                vol.Optional(CONF_PIN, default=opts.get(CONF_PIN, "")): str,
                vol.Optional(
                    CONF_FAST_POLL, default=opts.get(CONF_FAST_POLL, DEFAULT_FAST_POLL)
                ): vol.All(int, vol.Range(min=5, max=300)),
                vol.Optional(
                    CONF_SLOW_POLL, default=opts.get(CONF_SLOW_POLL, DEFAULT_SLOW_POLL)
                ): vol.All(int, vol.Range(min=10, max=600)),
                vol.Optional(
                    CONF_SERVICE_UUID, default=opts.get(CONF_SERVICE_UUID, "")
                ): str,
                vol.Optional(
                    CONF_WRITE_CHAR_UUID, default=opts.get(CONF_WRITE_CHAR_UUID, "")
                ): str,
                vol.Optional(
                    CONF_NOTIFY_CHAR_UUID, default=opts.get(CONF_NOTIFY_CHAR_UUID, "")
                ): str,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
