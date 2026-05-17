"""DataUpdateCoordinator for one Wallbox charger."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from bleak.exc import BleakError
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from bleak_retry_connector import close_stale_connections, establish_connection

from .bapi import (
    GattLayout,
    is_car_connected,
    is_charging,
    is_locked_from_status,
    status_name,
)
from .client import (
    WallboxAuthError,
    WallboxBleClient,
    WallboxProtocolError,
)
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
    SLOW_EVERY_N_FAST,
)

_LOGGER = logging.getLogger(__name__)

# Number of consecutive failed polls before we surface "unavailable" to HA.
# At fast-poll=10s this is ~30s of tolerated outage before entities flip.
_FAILURE_TOLERANCE = 2


@dataclass
class WallboxState:
    """Bundle of all telemetry/settings exposed to entities."""

    connected: bool = False
    layout_name: str | None = None

    # r_dat
    status_code: int | None = None
    status: str = "Unknown"
    charging_power_kw: float | None = None
    current_l1: float | None = None
    current_l2: float | None = None
    current_l3: float | None = None
    set_current: int | None = None
    session_energy_kwh: float | None = None
    session_green_kwh: float | None = None
    session_grid_kwh: float | None = None
    session_discharge_kwh: float | None = None
    # r_dca
    voltage_l1: float | None = None
    voltage_l2: float | None = None
    voltage_l3: float | None = None
    power_l1: float | None = None
    power_l2: float | None = None
    power_l3: float | None = None
    house_current_l1: float | None = None
    house_current_l2: float | None = None
    house_current_l3: float | None = None
    lifetime_energy_kwh: float | None = None
    # r_sta
    locked: bool | None = None
    max_available_current: int | None = None
    ocpp_status: int | None = None
    phases_connection: int | None = None
    # settings
    auto_lock_enabled: bool | None = None
    auto_lock_time_s: int | None = None
    eco_mode: int | None = None
    eco_percent: int | None = None
    eco_enabled: bool | None = None
    power_sharing_dynamic: bool | None = None
    phase_switch_enabled: bool | None = None
    timezone: str | None = None
    halo_brightness: int | None = None
    halo_mode: int | None = None
    halo_time_s: int | None = None

    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_charging(self) -> bool:
        return is_charging(self.status_code, self.layout_name)

    @property
    def car_connected(self) -> bool:
        return is_car_connected(self.status_code, self.layout_name)


class WallboxBleCoordinator(DataUpdateCoordinator[WallboxState]):
    """Polls one charger; owns one WallboxBleClient."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.address: str = entry.unique_id or entry.data["address"]
        self._pin: str = entry.options.get(CONF_PIN, entry.data.get(CONF_PIN, "")) or ""
        self._layout_override = _layout_override_from_options(entry.options)
        self._fast_s = int(entry.options.get(CONF_FAST_POLL, DEFAULT_FAST_POLL))
        self._slow_s = int(entry.options.get(CONF_SLOW_POLL, DEFAULT_SLOW_POLL))
        self._tick = 0
        self.state = WallboxState()
        self.client: WallboxBleClient | None = None
        self._released = False  # True when the user has released the BLE link (phone-app mode)
        self._consecutive_failures = 0  # tolerate brief drops without flapping entities

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {self.address}",
            update_interval=timedelta(seconds=self._fast_s),
        )

    # -- public helpers ----------------------------------------------------

    @property
    def released(self) -> bool:
        return self._released

    async def async_release(self) -> None:
        """User-initiated release of the BLE link so the phone app can connect."""
        if self._released:
            return
        self._released = True
        _LOGGER.info("Releasing BLE link to %s (phone-app mode)", self.address)
        self.state.connected = False
        if self.client is not None:
            try:
                await self.client.disconnect()
            finally:
                self.client = None
        # Push an update so entities reflect unavailable state.
        self.async_update_listeners()

    async def async_acquire(self) -> None:
        """Resume polling + reconnect after a release."""
        if not self._released:
            return
        _LOGGER.info("Reacquiring BLE link to %s", self.address)
        self._released = False
        await self.async_request_refresh()

    async def async_send(self, met: str, par: Any = None) -> dict:
        """Send a write and refresh state shortly after."""
        if self._released:
            raise RuntimeError("BLE link is released; turn 'BLE in use' on to send commands")
        client = await self._ensure_connected()
        try:
            resp = await client.send(met, par)
        except WallboxAuthError as exc:
            raise ConfigEntryAuthFailed(str(exc)) from exc
        # Refresh on next tick so UI reflects new state quickly.
        self.hass.async_create_task(self.async_request_refresh())
        return resp

    # -- update loop -------------------------------------------------------

    async def _async_update_data(self) -> WallboxState:
        if self._released:
            # User has released the link for phone-app access; stay quiet.
            self.state.connected = False
            return self.state

        try:
            ok = await self._poll_once()
        except WallboxAuthError as exc:
            raise ConfigEntryAuthFailed(str(exc)) from exc

        if ok:
            self._consecutive_failures = 0
            self.state.connected = True
            return self.state

        # A poll failed. Drop any stale client so the next tick reconnects.
        if self.client is not None:
            try:
                await self.client.disconnect()
            finally:
                self.client = None

        self._consecutive_failures += 1
        if self._consecutive_failures <= _FAILURE_TOLERANCE:
            _LOGGER.debug(
                "Wallbox %s poll failed (%d/%d) — keeping last state",
                self.address,
                self._consecutive_failures,
                _FAILURE_TOLERANCE,
            )
            return self.state

        self.state.connected = False
        raise UpdateFailed(
            f"{self._consecutive_failures} consecutive polls failed on {self.address}"
        )

    async def _poll_once(self) -> bool:
        """Run one poll cycle. Return True on success, False on recoverable failure."""
        try:
            client = await self._ensure_connected()
        except WallboxAuthError:
            raise
        except (BleakError, TimeoutError, UpdateFailed) as exc:
            _LOGGER.debug("Wallbox %s reconnect failed: %s", self.address, exc)
            return False

        # Update layout_name BEFORE applying r_dat so status string + derived
        # booleans (is_charging, car_connected) use the right code map.
        self.state.layout_name = client.layout.name if client.layout else None

        self._tick += 1
        do_slow = (self._tick % SLOW_EVERY_N_FAST) == 1

        try:
            r_dat = await client.read("r_dat")
            _apply_r_dat(self.state, r_dat)
        except WallboxAuthError:
            raise
        except (WallboxProtocolError, BleakError, TimeoutError) as exc:
            _LOGGER.debug("Wallbox %s r_dat failed: %s", self.address, exc)
            return False

        try:
            r_dca = await client.read("r_dca")
            _apply_r_dca(self.state, r_dca)
        except (WallboxProtocolError, BleakError, TimeoutError):
            _LOGGER.debug("r_dca unavailable on %s", self.address)

        if do_slow:
            try:
                await _poll_slow(client, self.state)
            except (BleakError, TimeoutError):
                _LOGGER.debug("slow tier failed on %s", self.address)

        return True

    async def _ensure_connected(self) -> WallboxBleClient:
        if self.client is not None and self.client.is_connected:
            return self.client

        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is None:
            raise UpdateFailed(f"BLE device {self.address} not in range")

        await close_stale_connections(ble_device)

        client = WallboxBleClient(
            ble_device,
            pin=self._pin,
            layout_override=self._layout_override,
            disconnected_callback=self._on_disconnect,
        )
        await client.connect(establish=establish_connection)
        self.client = client
        return client

    def _on_disconnect(self) -> None:
        self.state.connected = False

    def detach_client(self) -> None:
        """Drop the reference without disconnecting (used during reload)."""
        self.client = None

    async def async_shutdown(self) -> None:  # type: ignore[override]
        if self.client is not None:
            try:
                await self.client.disconnect()
            finally:
                self.client = None
        await super().async_shutdown()


# ---------------------------------------------------------------------------
# State application helpers (pure functions, easy to unit-test later)
# ---------------------------------------------------------------------------

def _apply_r_dat(s: WallboxState, r: Any) -> None:
    if not isinstance(r, dict):
        return
    st = _to_int(r.get("st"))
    if st is not None:
        s.status_code = st
        s.status = status_name(st, s.layout_name)
        # Some firmwares (notably Pulsar Plus) don't populate r_sta.lock_status;
        # derive lock state from status code when it indicates LOCKED.
        from_st = is_locked_from_status(st, s.layout_name)
        if from_st:
            s.locked = True
    s.charging_power_kw = _to_float(r.get("cp"))
    s.current_l1 = _scaled(r.get("L1"), 0.1)
    s.current_l2 = _scaled(r.get("L2"), 0.1)
    s.current_l3 = _scaled(r.get("L3"), 0.1)
    cur = _to_int(r.get("cur"))
    if cur is not None:
        s.set_current = cur
    s.session_energy_kwh = _scaled(r.get("en"), 0.01)
    s.session_green_kwh = _scaled(r.get("gen"), 0.01)
    s.session_grid_kwh = _scaled(r.get("grid"), 0.01)
    s.session_discharge_kwh = _scaled(r.get("den"), 0.001)


def _apply_r_dca(s: WallboxState, r: Any) -> None:
    if not isinstance(r, dict):
        return
    s.voltage_l1 = _to_float(r.get("v1"))
    s.voltage_l2 = _to_float(r.get("v2"))
    s.voltage_l3 = _to_float(r.get("v3"))
    s.power_l1 = _to_float(r.get("p1"))
    s.power_l2 = _to_float(r.get("p2"))
    s.power_l3 = _to_float(r.get("p3"))
    s.house_current_l1 = _scaled(r.get("c1"), 0.1)
    s.house_current_l2 = _scaled(r.get("c2"), 0.1)
    s.house_current_l3 = _scaled(r.get("c3"), 0.1)
    s.lifetime_energy_kwh = _scaled(r.get("e"), 0.001)


def _apply_r_sta(s: WallboxState, r: Any) -> None:
    if not isinstance(r, dict):
        return
    st = _to_int(r.get("charger_status"))
    if st is not None and s.status_code is None:
        s.status_code = st
        s.status = status_name(st, s.layout_name)
    lock = r.get("lock_status")
    if lock is not None:
        s.locked = bool(int(lock))
    mac = _to_int(r.get("max_available_current"))
    if mac is not None:
        s.max_available_current = mac
    s.ocpp_status = _to_int(r.get("ocpp_status"))
    s.phases_connection = _to_int(r.get("phases_connection"))


async def _poll_slow(client: WallboxBleClient, s: WallboxState) -> None:
    """Slow-tier reads; tolerate per-call errors."""
    for met, applier in (
        ("r_sta", _apply_r_sta),
        ("g_alo", _apply_g_alo),
        ("g_ecos", _apply_g_ecos),
        ("g_psh", _apply_g_psh),
        ("g_phsw", _apply_g_phsw),
        ("g_tzn", _apply_g_tzn),
        ("g_halocfg", _apply_g_halocfg),
    ):
        try:
            r = await client.read(met)
            applier(s, r)
        except (WallboxProtocolError, BleakError, TimeoutError):
            _LOGGER.debug("slow read %s failed on %s", met, client.address)


def _apply_g_alo(s: WallboxState, r: Any) -> None:
    if isinstance(r, dict):
        en = r.get("enabled")
        if en is not None:
            s.auto_lock_enabled = bool(int(en))
        t = _to_int(r.get("time"))
        if t is not None:
            s.auto_lock_time_s = t
    elif isinstance(r, (int, bool)):
        s.auto_lock_enabled = bool(int(r))


def _apply_g_ecos(s: WallboxState, r: Any) -> None:
    if not isinstance(r, dict):
        return
    esm = _to_int(r.get("esm"))
    if esm is not None:
        s.eco_mode = esm
    esp = _to_int(r.get("esp"))
    if esp is not None:
        s.eco_percent = esp
    ese = r.get("ese")
    if ese is not None:
        s.eco_enabled = bool(int(ese))


def _apply_g_psh(s: WallboxState, r: Any) -> None:
    if isinstance(r, dict):
        dyps = r.get("dyps")
        if dyps is not None:
            s.power_sharing_dynamic = bool(int(dyps))


def _apply_g_phsw(s: WallboxState, r: Any) -> None:
    if isinstance(r, dict):
        en = r.get("enabled")
        if en is not None:
            s.phase_switch_enabled = bool(int(en))


def _apply_g_tzn(s: WallboxState, r: Any) -> None:
    if isinstance(r, dict):
        tz = r.get("timezone")
        if isinstance(tz, str):
            s.timezone = tz


def _apply_g_halocfg(s: WallboxState, r: Any) -> None:
    if not isinstance(r, dict):
        return
    b = _to_int(r.get("bright"))
    if b is not None:
        s.halo_brightness = b
    m = _to_int(r.get("mode"))
    if m is not None:
        s.halo_mode = m
    t = _to_int(r.get("time_s"))
    if t is not None:
        s.halo_time_s = t


def _to_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _scaled(v: Any, factor: float) -> float | None:
    f = _to_float(v)
    if f is None:
        return None
    return f * factor


def _layout_override_from_options(opts: dict) -> GattLayout | None:
    svc = opts.get(CONF_SERVICE_UUID)
    wch = opts.get(CONF_WRITE_CHAR_UUID)
    nch = opts.get(CONF_NOTIFY_CHAR_UUID)
    if svc and wch and nch:
        return GattLayout(
            name="override",
            service=svc,
            write_char=wch,
            notify_char=nch,
            write_without_response=True,
        )
    return None
