"""BLE client for Wallbox chargers.

Owns one BleakClient and an inbound notification reassembler. Auto-probes
GATT layout (Pulsar MAX single-char, Pulsar Plus two-char, or override).
Serializes commands behind an asyncio.Lock; routes responses by `id`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from .bapi import (
    GattLayout,
    KNOWN_LAYOUTS,
    ResponseParser,
    build_cmd,
)

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = 5.0
CONNECT_TIMEOUT = 20.0


class WallboxAuthError(Exception):
    """Raised when the charger rejects the configured PIN."""


class WallboxProtocolError(Exception):
    """Raised when the charger returns an `error` body."""


@dataclass
class _Pending:
    future: asyncio.Future[dict]


class WallboxBleClient:
    """Low-level BLE protocol client."""

    def __init__(
        self,
        ble_device: BLEDevice,
        *,
        pin: str | None = None,
        layout_override: GattLayout | None = None,
        disconnected_callback: Callable[[], None] | None = None,
    ) -> None:
        self._ble_device = ble_device
        self._pin = pin or ""
        self._layout_override = layout_override
        self._on_disconnect = disconnected_callback

        self._client: BleakClient | None = None
        self._layout: GattLayout | None = None
        self._parser = ResponseParser()
        self._pending: dict[int, _Pending] = {}
        self._lock = asyncio.Lock()
        self._next_id = 1
        self._connected = False

    # -- public state ------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._connected and self._client is not None and self._client.is_connected

    @property
    def layout(self) -> GattLayout | None:
        return self._layout

    @property
    def address(self) -> str:
        return self._ble_device.address

    # -- lifecycle ---------------------------------------------------------

    async def connect(
        self,
        establish: Callable[..., Awaitable[BleakClient]] | None = None,
    ) -> None:
        """Connect, probe GATT, subscribe, and authenticate.

        `establish` is the connector callable (typically
        bleak_retry_connector.establish_connection). If None, we fall back
        to plain BleakClient — useful for the standalone CLI / tests.
        """
        if self.is_connected:
            return

        if establish is None:
            client = BleakClient(
                self._ble_device,
                disconnected_callback=self._handle_disconnect,
                timeout=CONNECT_TIMEOUT,
            )
            await client.connect()
        else:
            client = await establish(
                BleakClient,
                self._ble_device,
                "wallbox_ble",
                self._handle_disconnect,
            )
        self._client = client

        try:
            self._layout = await self._resolve_layout(client)
            _LOGGER.debug("Wallbox %s GATT layout: %s", self.address, self._layout.name)
            await client.start_notify(self._layout.notify_char, self._handle_notify)
            self._connected = True
            await self._authenticate()
        except Exception:
            with _suppress():
                await client.disconnect()
            self._client = None
            self._layout = None
            self._connected = False
            raise

    async def disconnect(self) -> None:
        client = self._client
        self._connected = False
        if client is None:
            return
        with _suppress():
            if self._layout is not None and client.is_connected:
                await client.stop_notify(self._layout.notify_char)
        with _suppress():
            await client.disconnect()
        self._client = None
        self._layout = None
        self._parser.reset()
        self._fail_all_pending(BleakError("disconnected"))

    def _handle_disconnect(self, _client: BleakClient) -> None:
        _LOGGER.debug("Wallbox %s disconnected", self.address)
        self._connected = False
        self._fail_all_pending(BleakError("disconnected"))
        if self._on_disconnect is not None:
            try:
                self._on_disconnect()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("disconnect callback raised")

    # -- GATT probe --------------------------------------------------------

    async def _resolve_layout(self, client: BleakClient) -> GattLayout:
        if self._layout_override is not None:
            return self._layout_override
        services = client.services
        # bleak >=0.22: services is BleakGATTServiceCollection (always populated post-connect)
        existing = {s.uuid.lower() for s in services}
        for layout in KNOWN_LAYOUTS:
            if layout.service.lower() in existing:
                # Verify the characteristics actually exist.
                chars = {c.uuid.lower() for s in services for c in s.characteristics}
                if (
                    layout.write_char.lower() in chars
                    and layout.notify_char.lower() in chars
                ):
                    return layout
        raise BleakError(
            f"No known Wallbox GATT layout found on {self.address}. "
            "Available services: " + ", ".join(sorted(existing))
        )

    # -- request/response --------------------------------------------------

    def _handle_notify(self, _char: Any, data: bytearray) -> None:
        for obj in self._parser.feed(bytes(data)):
            self._dispatch(obj)

    def _dispatch(self, obj: dict) -> None:
        rid = obj.get("id")
        if rid is None or rid not in self._pending:
            _LOGGER.debug("Unsolicited frame from %s: %s", self.address, obj)
            return
        pending = self._pending.pop(rid)
        if not pending.future.done():
            pending.future.set_result(obj)

    def _fail_all_pending(self, exc: BaseException) -> None:
        for rid, pending in list(self._pending.items()):
            if not pending.future.done():
                pending.future.set_exception(exc)
            del self._pending[rid]

    async def send(
        self,
        met: str,
        par: Any = None,
        *,
        timeout: float = REQUEST_TIMEOUT,
    ) -> dict:
        """Send one BAPI command, await response by id. Returns response dict."""
        if not self.is_connected or self._client is None or self._layout is None:
            raise BleakError("not connected")

        async with self._lock:
            req_id = self._next_id
            self._next_id = (self._next_id % 0x7FFFFFFF) + 1

            loop = asyncio.get_running_loop()
            fut: asyncio.Future[dict] = loop.create_future()
            self._pending[req_id] = _Pending(future=fut)

            frame_bytes = build_cmd(met, par, req_id)
            try:
                await self._client.write_gatt_char(
                    self._layout.write_char,
                    frame_bytes,
                    response=not self._layout.write_without_response,
                )
            except BleakError:
                # Some Pulsar Plus radios require write-with-response — retry once.
                if self._layout.write_without_response:
                    await self._client.write_gatt_char(
                        self._layout.write_char,
                        frame_bytes,
                        response=True,
                    )
                else:
                    self._pending.pop(req_id, None)
                    raise

            try:
                resp = await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError as exc:
                self._pending.pop(req_id, None)
                raise BleakError(f"timeout waiting for {met} (id={req_id})") from exc

        if "error" in resp:
            msg = resp["error"].get("message") if isinstance(resp["error"], dict) else str(resp["error"])
            raise WallboxProtocolError(f"{met}: {msg}")
        return resp

    async def read(self, met: str) -> Any:
        """Convenience: send a read with par=null and return the `r` body."""
        resp = await self.send(met, None)
        return resp.get("r")

    # -- PIN auth ----------------------------------------------------------

    async def _authenticate(self) -> None:
        """Run read_pin and, if a PIN is set on the charger, send set_pin."""
        try:
            r = await self.read("read_pin")
        except WallboxProtocolError:
            # Some firmwares don't support read_pin — assume no PIN.
            return
        except BleakError:
            raise

        if not isinstance(r, dict):
            return
        charger_pin = r.get("pin") or ""
        version = r.get("version", 0)
        if not charger_pin:
            return  # open access

        if not self._pin:
            raise WallboxAuthError("charger has a PIN set; provide one in the integration options")
        try:
            await self.send("set_pin", {"pin": self._pin, "version": version})
        except WallboxProtocolError as exc:
            raise WallboxAuthError(str(exc)) from exc


class _suppress:
    """Tiny context manager replacement that swallows + logs exceptions."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is not None:
            _LOGGER.debug("suppressed: %r", exc)
        return True
