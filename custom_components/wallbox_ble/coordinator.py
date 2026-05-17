"""DataUpdateCoordinator for one Wallbox charger."""

from __future__ import annotations

import logging
import time
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
    CONF_INTERVAL_CHARGING,
    CONF_INTERVAL_CONNECTED,
    CONF_INTERVAL_IDLE,
    CONF_NOTIFY_CHAR_UUID,
    CONF_PIN,
    CONF_SERVICE_UUID,
    CONF_SLOW_POLL,
    CONF_WRITE_CHAR_UUID,
    DEFAULT_INTERVAL_CHARGING,
    DEFAULT_INTERVAL_CONNECTED,
    DEFAULT_INTERVAL_IDLE,
    DOMAIN,
    R_STA_INTERVAL_S,
)

_LOGGER = logging.getLogger(__name__)

# Number of consecutive failed polls before we surface "unavailable" to HA.
# At fast-poll=10s this is ~30s of tolerated outage before entities flip.
_FAILURE_TOLERANCE = 2

# Map a write op to the read method whose result reflects that write, so we
# can verify "did the setting take effect" without polling everything.
_READBACK_FOR_WRITE: dict[str, str | None] = {
    # Status-changing writes: r_dat readback (already triggered by every
    # async_send via async_request_refresh; no extra entry needed).
    "w_cha": None,
    "w_mxI": None,
    "w_lck": None,
    "rebot": None,
    "clr_sch": None,
    # Settings writes: read the matching getter on next tick.
    "s_ecos": "g_ecos",
    "s_alo": "g_alo",
    "s_phsw": "g_phsw",
    "s_psh": "g_psh",
    "s_halocfg": "g_halocfg",
    "s_tzn": "g_tzn",
    "set_pin": None,
}


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
        opts = entry.options
        self._pin: str = opts.get(CONF_PIN, entry.data.get(CONF_PIN, "")) or ""
        self._layout_override = _layout_override_from_options(opts)
        # Per-state poll cadence. Honor legacy CONF_FAST_POLL/SLOW_POLL keys
        # if present (older entries) so options carry over.
        self._interval_charging = int(
            opts.get(CONF_INTERVAL_CHARGING, opts.get(CONF_FAST_POLL, DEFAULT_INTERVAL_CHARGING))
        )
        self._interval_connected = int(
            opts.get(CONF_INTERVAL_CONNECTED, DEFAULT_INTERVAL_CONNECTED)
        )
        self._interval_idle = int(
            opts.get(CONF_INTERVAL_IDLE, opts.get(CONF_SLOW_POLL, DEFAULT_INTERVAL_IDLE))
        )
        self.state = WallboxState()
        self.client: WallboxBleClient | None = None
        self._released = False  # True when the user has released the BLE link (phone-app mode)
        self._consecutive_failures = 0  # tolerate brief drops without flapping entities
        self._last_settings_refresh = 0.0  # monotonic timestamp of last full settings sweep
        self._pending_readbacks: set[str] = set()  # settings to re-read after writes

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {self.address}",
            update_interval=timedelta(seconds=self._interval_idle),
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
        """Send a write and read back the affected state inline so the UI
        reflects the change within ~1 BLE round-trip (no debounce wait)."""
        if self._released:
            raise RuntimeError("BLE link is released; turn 'BLE in use' on to send commands")
        client = await self._ensure_connected()
        try:
            resp = await client.send(met, par)
        except WallboxAuthError as exc:
            raise ConfigEntryAuthFailed(str(exc)) from exc

        # Inline readback: read whichever endpoint reflects the write, apply
        # to state, then push to entities. Avoids the coordinator refresh
        # debouncer (which can delay the UI update by up to ~10s).
        readback = _READBACK_FOR_WRITE.get(met)
        try:
            if readback is not None:
                r = await client.read(readback)
                applier = _SETTING_APPLIERS.get(readback)
                if applier is not None:
                    applier(self.state, r)
            else:
                # w_cha / w_mxI / w_lck / clr_sch all affect r_dat's fields.
                # rebot / set_pin don't, but a stale r_dat read is harmless.
                r = await client.read("r_dat")
                _apply_r_dat(self.state, r)
        except (WallboxProtocolError, BleakError, TimeoutError) as exc:
            _LOGGER.debug(
                "inline readback after %s failed (%s); queuing for next tick",
                met,
                exc,
            )
            # Fall back to the deferred path so a transient failure still
            # gets corrected on the next scheduled poll.
            if readback is not None:
                self._pending_readbacks.add(readback)

        self.async_update_listeners()
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
        """Run one poll cycle. Return True on success, False on recoverable failure.

        Adaptive: only reads telemetry that's interesting for the current
        charger state. Settings (g_*) read once at startup, then only after a
        write of the matching setter. r_sta runs every R_STA_INTERVAL_S.
        """
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

        # 1. r_dat: always — that's what tells us whether the car is connected,
        #    charging, or idle. Drives every cadence decision below.
        try:
            r_dat = await client.read("r_dat")
            _apply_r_dat(self.state, r_dat)
        except WallboxAuthError:
            raise
        except (WallboxProtocolError, BleakError, TimeoutError) as exc:
            _LOGGER.debug("Wallbox %s r_dat failed: %s", self.address, exc)
            return False

        # 2. Slow tier (r_sta + r_dca + every g_* setting): every
        #    R_STA_INTERVAL_S so values can't go stale if changed externally
        #    (e.g. via the Wallbox phone app while we were idle).
        now = time.monotonic()
        if (now - self._last_settings_refresh) >= R_STA_INTERVAL_S:
            try:
                r = await client.read("r_sta")
                _apply_r_sta(self.state, r)
            except (WallboxProtocolError, BleakError, TimeoutError):
                _LOGGER.debug("r_sta unavailable on %s", self.address)
            try:
                r = await client.read("r_dca")
                _apply_r_dca(self.state, r)
            except (WallboxProtocolError, BleakError, TimeoutError):
                _LOGGER.debug("r_dca unavailable on %s", self.address)
            await _poll_settings(client, self.state, _SETTING_READS)
            # Any pending readbacks have just been satisfied implicitly.
            self._pending_readbacks.clear()
            self._last_settings_refresh = now
        elif self._pending_readbacks:
            mets = list(self._pending_readbacks)
            self._pending_readbacks.clear()
            await _poll_settings(client, self.state, mets)

        # 5. Adapt the next-tick interval to current state.
        self._update_interval_for_state()
        return True

    def _update_interval_for_state(self) -> None:
        """Adjust the poll interval based on the latest observed state."""
        if self.state.is_charging:
            new = self._interval_charging
        elif self.state.car_connected:
            new = self._interval_connected
        else:
            new = self._interval_idle
        if self.update_interval is None or self.update_interval.total_seconds() != new:
            self.update_interval = timedelta(seconds=new)
            _LOGGER.debug("Wallbox %s next interval: %ds", self.address, new)

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


_SETTING_APPLIERS = {
    "g_alo": None,  # filled in below
    "g_ecos": None,
    "g_psh": None,
    "g_phsw": None,
    "g_tzn": None,
    "g_halocfg": None,
}

# Order matters only for the once-at-startup walk.
_SETTING_READS = ("g_alo", "g_ecos", "g_psh", "g_phsw", "g_tzn", "g_halocfg")


async def _poll_settings(
    client: WallboxBleClient, s: WallboxState, mets: list[str] | tuple[str, ...]
) -> None:
    """Read a list of g_* setting endpoints; tolerate per-call errors."""
    for met in mets:
        applier = _SETTING_APPLIERS.get(met)
        if applier is None:
            continue
        try:
            r = await client.read(met)
            applier(s, r)
        except (WallboxProtocolError, BleakError, TimeoutError):
            _LOGGER.debug("setting read %s failed on %s", met, client.address)


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


_SETTING_APPLIERS["g_alo"] = _apply_g_alo
_SETTING_APPLIERS["g_ecos"] = _apply_g_ecos
_SETTING_APPLIERS["g_psh"] = _apply_g_psh
_SETTING_APPLIERS["g_phsw"] = _apply_g_phsw
_SETTING_APPLIERS["g_tzn"] = _apply_g_tzn
_SETTING_APPLIERS["g_halocfg"] = _apply_g_halocfg


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
