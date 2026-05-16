#!/usr/bin/env python3
"""Standalone Wallbox BLE CLI using the host's local Bluetooth adapter.

For ESPHome-proxy-based testing use tools/wallbox_esphome_test.py instead.

Usage:
    python -m tools.wallbox_cli scan
    python -m tools.wallbox_cli info <addr> [--pin 1234]
    python -m tools.wallbox_cli cmd  <addr> <met> [par-as-json] [--pin 1234]
    python -m tools.wallbox_cli monitor <addr> [--pin 1234] [--interval 10]
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

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

_REPO = pathlib.Path(__file__).resolve().parent.parent
_BAPI = _REPO / "custom_components" / "wallbox_ble" / "bapi.py"
_spec = importlib.util.spec_from_file_location("wallbox_ble_bapi", _BAPI)
bapi = importlib.util.module_from_spec(_spec)
sys.modules["wallbox_ble_bapi"] = bapi
_spec.loader.exec_module(bapi)

LOGGER = logging.getLogger("wallbox-cli")
NAME_RE = re.compile(r"^WB\d|wallbox", re.IGNORECASE)


class _Session:
    def __init__(self, client: BleakClient, layout) -> None:
        self.client = client
        self.layout = layout
        self.parser = bapi.ResponseParser()
        self.pending: dict[int, asyncio.Future] = {}
        self.lock = asyncio.Lock()
        self.next_id = 1

    def _on_notify(self, _char, data: bytearray) -> None:
        for obj in self.parser.feed(bytes(data)):
            rid = obj.get("id")
            fut = self.pending.pop(rid, None) if rid is not None else None
            if fut and not fut.done():
                fut.set_result(obj)

    async def start(self) -> None:
        await self.client.start_notify(self.layout.notify_char, self._on_notify)

    async def stop(self) -> None:
        try:
            await self.client.stop_notify(self.layout.notify_char)
        except Exception:  # noqa: BLE001
            pass

    async def send(self, met: str, par: Any = None, timeout: float = 5.0) -> dict:
        async with self.lock:
            rid = self.next_id
            self.next_id += 1
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self.pending[rid] = fut
            frame = bapi.build_cmd(met, par, rid)
            LOGGER.debug(">> %s par=%r id=%d hex=%s", met, par, rid, frame.hex())
            await self.client.write_gatt_char(
                self.layout.write_char,
                frame,
                response=not self.layout.write_without_response,
            )
            try:
                resp = await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                self.pending.pop(rid, None)
                raise
            LOGGER.debug("<< %s", json.dumps(resp))
            return resp


async def _probe_layout(client: BleakClient):
    services = client.services
    existing = {s.uuid.lower() for s in services}
    chars = {c.uuid.lower() for s in services for c in s.characteristics}
    for layout in bapi.KNOWN_LAYOUTS:
        if (
            layout.service.lower() in existing
            and layout.write_char.lower() in chars
            and layout.notify_char.lower() in chars
        ):
            return layout
    return None


async def _connect(addr: str, pin: str) -> tuple[BleakClient, _Session]:
    device = await BleakScanner.find_device_by_address(addr, timeout=10.0)
    if device is None:
        raise SystemExit(f"Device {addr} not found")
    client = BleakClient(device)
    await client.connect()
    layout = await _probe_layout(client)
    if layout is None:
        await client.disconnect()
        raise SystemExit(f"No Wallbox GATT layout on {addr}")
    LOGGER.info("Connected; layout=%s", layout.name)
    session = _Session(client, layout)
    await session.start()

    resp = await session.send("read_pin")
    r = resp.get("r") if isinstance(resp, dict) else None
    if isinstance(r, dict) and r.get("pin"):
        if not pin:
            LOGGER.warning("Charger has a PIN set but none provided; commands may be rejected")
        else:
            await session.send("set_pin", {"pin": pin, "version": r.get("version", 0)})
            LOGGER.info("Authenticated with PIN")
    return client, session


async def cmd_scan(args: argparse.Namespace) -> int:
    LOGGER.info("Scanning %.1fs ...", args.seconds)
    seen: list[BLEDevice] = []
    devices = await BleakScanner.discover(timeout=args.seconds, return_adv=True)
    for addr, (device, adv) in devices.items():
        name = device.name or adv.local_name or ""
        if not args.all and not NAME_RE.search(name):
            continue
        print(f"{addr}  rssi={adv.rssi}  name={name!r}")
        seen.append(device)
    if not seen and not args.all:
        print("(no matches; pass --all to list every device)")
    return 0


async def cmd_info(args: argparse.Namespace) -> int:
    client, session = await _connect(args.address, args.pin)
    try:
        for met in ("r_dat", "r_sta", "r_dca", "r_ver"):
            try:
                resp = await session.send(met)
                print(f"== {met} ==")
                print(json.dumps(resp, indent=2, default=str))
                if met == "r_dat":
                    st = resp.get("r", {}).get("st") if isinstance(resp, dict) else None
                    print(f"  status: {bapi.status_name(st)}")
            except asyncio.TimeoutError:
                print(f"== {met} == TIMEOUT")
    finally:
        await session.stop()
        await client.disconnect()
    return 0


async def cmd_cmd(args: argparse.Namespace) -> int:
    par: Any
    if args.par is None or args.par.lower() == "null":
        par = None
    else:
        try:
            par = json.loads(args.par)
        except json.JSONDecodeError:
            par = args.par  # fall back to string
    client, session = await _connect(args.address, args.pin)
    try:
        resp = await session.send(args.met, par)
        print(json.dumps(resp, indent=2, default=str))
    finally:
        await session.stop()
        await client.disconnect()
    return 0


async def cmd_monitor(args: argparse.Namespace) -> int:
    client, session = await _connect(args.address, args.pin)
    last_status: int | None = None
    try:
        while True:
            try:
                resp = await session.send("r_dat")
                r = resp.get("r", {}) if isinstance(resp, dict) else {}
                st = r.get("st")
                if st != last_status:
                    print(f"status={bapi.status_name(st)} ({st})  cur={r.get('cur')}  cp={r.get('cp')}kW  L1/2/3={r.get('L1')}/{r.get('L2')}/{r.get('L3')}")
                    last_status = st
            except asyncio.TimeoutError:
                LOGGER.warning("r_dat timeout")
            await asyncio.sleep(args.interval)
    finally:
        await session.stop()
        await client.disconnect()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="subcmd", required=True)

    p_scan = sub.add_parser("scan")
    p_scan.add_argument("--seconds", type=float, default=6.0)
    p_scan.add_argument("--all", action="store_true", help="list every device, not just Wallbox matches")
    p_scan.set_defaults(fn=cmd_scan)

    p_info = sub.add_parser("info")
    p_info.add_argument("address")
    p_info.add_argument("--pin", default="")
    p_info.set_defaults(fn=cmd_info)

    p_cmd = sub.add_parser("cmd")
    p_cmd.add_argument("address")
    p_cmd.add_argument("met")
    p_cmd.add_argument("par", nargs="?")
    p_cmd.add_argument("--pin", default="")
    p_cmd.set_defaults(fn=cmd_cmd)

    p_mon = sub.add_parser("monitor")
    p_mon.add_argument("address")
    p_mon.add_argument("--pin", default="")
    p_mon.add_argument("--interval", type=float, default=10.0)
    p_mon.set_defaults(fn=cmd_monitor)

    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(args.fn(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
