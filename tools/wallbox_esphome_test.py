#!/usr/bin/env python3
"""Connect to an ESPHome BLE-proxy device and interrogate a Wallbox charger.

This harness talks directly to the ESPHome native API (no Home Assistant in
the loop). It scans for nearby BLE advertisements via the proxy, finds a
device whose name matches the Wallbox patterns, connects, probes the GATT
layout, subscribes to notifications, and issues a sequence of BAPI reads.

Usage:
    python -m tools.wallbox_esphome_test \\
        --host 192.168.1.50 --password "" \\
        [--mac AA:BB:CC:DD:EE:FF] [--pin 1234] [--scan-seconds 8]

The script depends only on `aioesphomeapi` and the bapi.py module from this
repo. No HA, no bleak.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import pathlib
import re
import sys
from typing import Any

from aioesphomeapi import (
    APIClient,
    BluetoothGATTServices,
    BluetoothLEAdvertisement,
    BluetoothProxyFeature,
)

# Load bapi.py directly so we don't need HA imports.
_REPO = pathlib.Path(__file__).resolve().parent.parent
_BAPI = _REPO / "custom_components" / "wallbox_ble" / "bapi.py"
_spec = importlib.util.spec_from_file_location("wallbox_ble_bapi", _BAPI)
bapi = importlib.util.module_from_spec(_spec)
sys.modules["wallbox_ble_bapi"] = bapi
_spec.loader.exec_module(bapi)

LOGGER = logging.getLogger("wallbox-esphome-test")

NAME_RE = re.compile(r"^WB\d|wallbox", re.IGNORECASE)


def _mac_str_to_int(mac: str) -> int:
    return int(mac.replace(":", "").replace("-", ""), 16)


def _mac_int_to_str(mac: int) -> str:
    h = f"{mac:012X}"
    return ":".join(h[i : i + 2] for i in range(0, 12, 2))


def _parse_adv_name(data: bytes) -> str:
    """Extract the local name from a raw BLE advertising-data payload."""
    i = 0
    while i < len(data):
        length = data[i]
        if length == 0 or i + 1 + length > len(data):
            break
        ad_type = data[i + 1]
        ad_value = data[i + 2 : i + 1 + length]
        if ad_type in (0x08, 0x09):  # shortened / complete local name
            try:
                return ad_value.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                return ""
        i += 1 + length
    return ""


async def scan_for_wallbox(
    client: APIClient,
    *,
    target_mac: int | None,
    seconds: float,
    use_raw: bool,
) -> tuple[int, str, int | None] | None:
    """Return (address, name, address_type) of first matching device, or None."""
    found: asyncio.Future[tuple[int, str, int | None]] = asyncio.get_running_loop().create_future()
    seen: dict[int, str] = {}

    def _consider(address: int, name: str, rssi: int, addr_type: int | None) -> None:
        if found.done():
            return
        if target_mac is not None:
            if address == target_mac:
                found.set_result((address, name or _mac_int_to_str(address), addr_type))
            return
        if NAME_RE.search(name or ""):
            LOGGER.info("Match: %s @ %s rssi=%d type=%s", name, _mac_int_to_str(address), rssi, addr_type)
            found.set_result((address, name, addr_type))
            return
        if address not in seen:
            seen[address] = name
            LOGGER.debug("seen %s (%s) rssi=%d", name, _mac_int_to_str(address), rssi)

    def _cb_parsed(adv: BluetoothLEAdvertisement) -> None:
        _consider(adv.address, adv.name or "", adv.rssi, getattr(adv, "address_type", None))

    def _cb_raw(advs) -> None:
        for r in advs.advertisements:
            _consider(r.address, _parse_adv_name(bytes(r.data)), r.rssi, getattr(r, "address_type", None))

    if use_raw:
        unsub = client.subscribe_bluetooth_le_raw_advertisements(_cb_raw)
    else:
        unsub = client.subscribe_bluetooth_le_advertisements(_cb_parsed)
    try:
        try:
            return await asyncio.wait_for(found, timeout=seconds)
        except asyncio.TimeoutError:
            LOGGER.error(
                "No matching device after %.1fs; saw %d total advertisers", seconds, len(seen)
            )
            for addr, name in seen.items():
                LOGGER.error("  %s  %s", _mac_int_to_str(addr), name or "<no name>")
            return None
    finally:
        unsub()


def _find_layout(services: BluetoothGATTServices):
    """Return (layout, write_handle, notify_handle) or None."""
    svc_by_uuid: dict[str, list] = {}
    for svc in services.services:
        svc_by_uuid.setdefault(svc.uuid.lower(), []).append(svc)

    for layout in bapi.KNOWN_LAYOUTS:
        candidates = svc_by_uuid.get(layout.service.lower(), [])
        for svc in candidates:
            write_h: int | None = None
            notify_h: int | None = None
            for ch in svc.characteristics:
                u = ch.uuid.lower()
                if u == layout.write_char.lower():
                    write_h = ch.handle
                if u == layout.notify_char.lower():
                    notify_h = ch.handle
            if write_h is not None and notify_h is not None:
                return layout, write_h, notify_h
    return None


class WallboxProxySession:
    """A serialized request/response channel over the ESPHome BLE proxy."""

    def __init__(self, client: APIClient, address: int, layout, write_h: int, notify_h: int) -> None:
        self.client = client
        self.address = address
        self.layout = layout
        self.write_h = write_h
        self.notify_h = notify_h
        self.parser = bapi.ResponseParser()
        self.pending: dict[int, asyncio.Future[dict]] = {}
        self.lock = asyncio.Lock()
        self.next_id = 1

    def on_notify(self, _handle: int, data: bytearray) -> None:
        for obj in self.parser.feed(bytes(data)):
            rid = obj.get("id")
            fut = self.pending.pop(rid, None) if rid is not None else None
            if fut and not fut.done():
                fut.set_result(obj)
            else:
                LOGGER.debug("unsolicited frame: %s", obj)

    async def send(self, met: str, par: Any = None, timeout: float = 5.0) -> dict:
        async with self.lock:
            rid = self.next_id
            self.next_id += 1
            fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
            self.pending[rid] = fut
            frame = bapi.build_cmd(met, par, rid)
            LOGGER.debug(">> %s par=%r id=%d  bytes=%s", met, par, rid, frame.hex())
            await self.client.bluetooth_gatt_write(
                self.address,
                self.write_h,
                frame,
                response=not self.layout.write_without_response,
            )
            try:
                resp = await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                self.pending.pop(rid, None)
                raise
            LOGGER.debug("<< %s -> %s", met, json.dumps(resp))
            return resp


async def run(args: argparse.Namespace) -> int:
    target_mac = _mac_str_to_int(args.mac) if args.mac else None

    client = APIClient(
        args.host,
        args.port,
        args.password or "",
        noise_psk=args.psk or None,
    )
    LOGGER.info("Connecting to ESPHome proxy %s:%d ...", args.host, args.port)
    await client.connect(login=True)

    try:
        device_info = await client.device_info()
        LOGGER.info("Proxy: %s (model %s, esphome %s)", device_info.name, device_info.model, device_info.esphome_version)

        feat = device_info.bluetooth_proxy_feature_flags
        if not (feat & BluetoothProxyFeature.ACTIVE_CONNECTIONS):
            LOGGER.error(
                "This ESPHome device does not advertise BLUETOOTH_PROXY_ACTIVE_CONNECTIONS. "
                "Flash it with `esp32_ble_tracker:` + `bluetooth_proxy: active: true`."
            )
            return 2

        # subscribe_bluetooth_connections_free returns an unsub callable synchronously.
        free = client.subscribe_bluetooth_connections_free(lambda *a, **kw: None)

        use_raw = bool(feat & BluetoothProxyFeature.RAW_ADVERTISEMENTS)
        LOGGER.info(
            "Scanning for Wallbox (up to %.1fs, %s adverts) ...",
            args.scan_seconds,
            "raw" if use_raw else "parsed",
        )
        match = await scan_for_wallbox(
            client, target_mac=target_mac, seconds=args.scan_seconds, use_raw=use_raw
        )
        if match is None:
            if target_mac is not None:
                LOGGER.warning(
                    "Scan turned up nothing; trying direct connect to %s with default "
                    "address_type=%d (override with --address-type if it fails)",
                    _mac_int_to_str(target_mac),
                    args.address_type,
                )
                match = (target_mac, _mac_int_to_str(target_mac), args.address_type)
            else:
                return 3
        address, name, addr_type = match
        LOGGER.info("Connecting to %s (%s) addr_type=%s ...", _mac_int_to_str(address), name, addr_type)

        conn_state: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

        def _on_state(connected: bool, mtu: int, error: int) -> None:
            LOGGER.info("BLE connection: connected=%s mtu=%d error=%d", connected, mtu, error)
            if connected and not conn_state.done():
                conn_state.set_result(True)
            if not connected and not conn_state.done():
                conn_state.set_exception(
                    RuntimeError(f"connection failed; error code {error}")
                )

        unsub_conn = await client.bluetooth_device_connect(
            address,
            _on_state,
            timeout=args.connect_timeout,
            disconnect_timeout=args.connect_timeout,
            feature_flags=feat,
            has_cache=False,
            address_type=addr_type if addr_type is not None else 0,
        )

        try:
            await asyncio.wait_for(conn_state, timeout=args.connect_timeout + 10)

            LOGGER.info("Reading GATT services ...")
            services = await client.bluetooth_gatt_get_services(address)
            found = _find_layout(services)
            if found is None:
                LOGGER.error("No known Wallbox GATT layout. Services seen:")
                for svc in services.services:
                    LOGGER.error("  service %s (h=%d)", svc.uuid, svc.handle)
                    for ch in svc.characteristics:
                        LOGGER.error("    char %s (h=%d, props=0x%02x)", ch.uuid, ch.handle, ch.properties)
                return 4
            layout, write_h, notify_h = found
            LOGGER.info(
                "Layout: %s  write_handle=%d  notify_handle=%d", layout.name, write_h, notify_h
            )

            session = WallboxProxySession(client, address, layout, write_h, notify_h)
            stop_notify, _cancel_pending = await client.bluetooth_gatt_start_notify(
                address, notify_h, session.on_notify
            )

            try:
                # PIN check + optional set_pin
                LOGGER.info("=== read_pin ===")
                resp = await session.send("read_pin")
                r = resp.get("r", {}) if isinstance(resp, dict) else {}
                print("read_pin:", json.dumps(resp, indent=2))
                if isinstance(r, dict) and r.get("pin"):
                    if not args.pin:
                        LOGGER.warning(
                            "Charger has a PIN (%s); pass --pin to authenticate. "
                            "Continuing in read-only mode.", r.get("pin")
                        )
                    else:
                        LOGGER.info("=== set_pin ===")
                        sp = await session.send(
                            "set_pin", {"pin": args.pin, "version": r.get("version", 0)}
                        )
                        print("set_pin:", json.dumps(sp, indent=2))

                for met in ("r_dat", "r_sta", "r_dca", "r_ver"):
                    LOGGER.info("=== %s ===", met)
                    try:
                        resp = await session.send(met)
                        print(f"{met}:", json.dumps(resp, indent=2, default=str))
                        if met == "r_dat":
                            st = resp.get("r", {}).get("st") if isinstance(resp, dict) else None
                            print(f"  -> status: {bapi.status_name(st)}")
                    except asyncio.TimeoutError:
                        LOGGER.error("%s: timeout", met)
                    except Exception:  # noqa: BLE001
                        LOGGER.exception("%s: failed", met)

                if args.send:
                    met, _, par_text = args.send.partition("=")
                    par: Any
                    if par_text == "" or par_text.lower() == "null":
                        par = None
                    else:
                        try:
                            par = json.loads(par_text)
                        except json.JSONDecodeError:
                            par = par_text
                    LOGGER.info("=== custom send: met=%s par=%r ===", met, par)
                    resp = await session.send(met, par)
                    print(f"{met}:", json.dumps(resp, indent=2))

            finally:
                try:
                    await stop_notify()
                except Exception:  # noqa: BLE001
                    LOGGER.debug("stop_notify failed", exc_info=True)
        finally:
            try:
                unsub_conn()
            except Exception:  # noqa: BLE001
                pass
            try:
                await client.bluetooth_device_disconnect(address)
            except Exception:  # noqa: BLE001
                LOGGER.debug("disconnect failed", exc_info=True)
            try:
                free()
            except Exception:  # noqa: BLE001
                pass

    finally:
        await client.disconnect()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", required=True, help="ESPHome proxy host or IP")
    ap.add_argument("--port", type=int, default=6053, help="ESPHome native API port (default 6053)")
    ap.add_argument("--password", default="", help="ESPHome API password (if set)")
    ap.add_argument(
        "--psk",
        default="",
        help="Noise PSK (base64) if the proxy uses encryption (api: encryption: key:)",
    )
    ap.add_argument("--mac", help="If scan does not find a Wallbox, fall back to this MAC")
    ap.add_argument(
        "--address-type",
        type=int,
        default=0,
        help="BLE address type for direct connect (0=public, 1=random). Default 0.",
    )
    ap.add_argument("--pin", default="", help="Wallbox BLE PIN, if configured")
    ap.add_argument("--scan-seconds", type=float, default=8.0)
    ap.add_argument("--connect-timeout", type=float, default=60.0)
    ap.add_argument(
        "--send",
        default="",
        help=(
            "Extra command to execute after the read sequence, format: "
            "'met=<json-par>'. Example: 'w_mxI=10' or 'w_lck=0' or 'r_log=3'."
        ),
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Quiet aioesphomeapi noise unless verbose.
    if not args.verbose:
        logging.getLogger("aioesphomeapi").setLevel(logging.WARNING)

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
