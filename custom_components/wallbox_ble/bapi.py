"""Pure Wallbox BAPI wire-protocol layer.

No HA, no bleak — just framing, parsing, and constants. This module is the
spec, derived from the ESP32 reference at github.com/botts7/esp32-wallbox
(src/bapi.cpp, src/wb_mqtt.cpp). It is the only file allowed to be unit-
tested without a real charger.

Request frame:
    b"EaE" + length + json_payload + checksum
where:
    - length is 1 byte if len(payload) < 256
    - else: 0x00 + ascii_decimal_len + 0x00
    - checksum = sum(everything_so_far) & 0xFF

Response: raw concatenated JSON across BLE notifications, reassembled via
brace-depth state machine (responses are NOT framed).
"""

from __future__ import annotations

import json
from dataclasses import dataclass


HEADER = b"EaE"


def frame(payload: bytes) -> bytes:
    """Wrap a JSON payload in the BAPI frame."""
    plen = len(payload)
    if plen < 256:
        prefix = HEADER + bytes([plen])
    else:
        prefix = HEADER + b"\x00" + str(plen).encode("ascii") + b"\x00"
    body = prefix + payload
    cs = sum(body) & 0xFF
    return body + bytes([cs])


def build_cmd(met: str, par, req_id: int) -> bytes:
    """Build a framed BAPI command.

    `par` follows the ESP32 convention: None -> JSON null, otherwise rendered
    with compact JSON. Strings are quoted, ints/bools/objects passed through.
    """
    if par is None:
        par_text = "null"
    elif isinstance(par, (int, float, bool)) and not isinstance(par, bool):
        par_text = json.dumps(par)
    elif isinstance(par, bool):
        par_text = "true" if par else "false"
    elif isinstance(par, str):
        # ESP32 sometimes passes ints as bare strings ("1", "6", "null").
        # If it looks like JSON already (number / null / object / array), inline it.
        s = par.strip()
        if s == "null" or s == "true" or s == "false":
            par_text = s
        elif s and (s[0] in "{[-0123456789" or s.replace(".", "", 1).isdigit()):
            try:
                json.loads(s)
                par_text = s
            except ValueError:
                par_text = json.dumps(par)
        else:
            par_text = json.dumps(par)
    else:
        par_text = json.dumps(par, separators=(",", ":"))
    json_text = '{"met":"' + met + '","par":' + par_text + ',"id":' + str(req_id) + "}"
    return frame(json_text.encode("utf-8"))


class ResponseParser:
    """Reassemble JSON objects from a stream of notification chunks.

    Tracks brace depth, string state, and `\\` escapes. Yields each complete
    top-level object (as parsed dict) via `feed()`.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._depth = 0
        self._in_string = False
        self._escape = False
        self._started = False

    def reset(self) -> None:
        self._buf.clear()
        self._depth = 0
        self._in_string = False
        self._escape = False
        self._started = False

    def feed(self, data: bytes) -> list[dict]:
        """Append data; return any complete JSON objects parsed out."""
        out: list[dict] = []
        for byte in data:
            c = chr(byte)
            # Skip anything before the first '{' (defensive against garbage / re-syncs).
            if not self._started:
                if c != "{":
                    continue
                self._started = True
            self._buf.append(byte)
            if self._escape:
                self._escape = False
                continue
            if self._in_string:
                if c == "\\":
                    self._escape = True
                elif c == '"':
                    self._in_string = False
                continue
            if c == '"':
                self._in_string = True
                continue
            if c == "{":
                self._depth += 1
            elif c == "}":
                self._depth -= 1
                if self._depth == 0:
                    try:
                        obj = json.loads(self._buf.decode("utf-8"))
                        if isinstance(obj, dict):
                            out.append(obj)
                    except (UnicodeDecodeError, ValueError):
                        pass
                    self._buf.clear()
                    self._started = False
        return out


# --- Status code maps ---
#
# Pulsar Plus and Pulsar MAX use COMPLETELY DIFFERENT code-to-meaning
# mappings. The same integer means different things on different firmwares.
# Pick the map by GATT layout name.

# Pulsar MAX (from botts7/esp32-wallbox src/wb_mqtt.cpp:476-484 + wb_web.cpp:459)
STATUS_MAP_PULSAR_MAX: dict[int, str] = {
    0: "Disconnected",
    1: "Connected",
    2: "Charging",
    3: "Paused",
    4: "Scheduled",
    5: "Discharging",
    6: "Error",
    7: "Disconnected",
    8: "Locked",
    9: "Updating",
    10: "Queue (Power)",
    13: "Waiting for Car",
    14: "Error",
    16: "Ready",
    17: "Connected",
    18: "Waiting for Schedule",
    19: "Scheduled",
    20: "Charging",
    21: "Charge Complete",
    22: "Paused by User",
    23: "Queue (Power Share)",
    24: "Queue (Eco Smart)",
    25: "Waiting for Schedule",
    26: "Discharging",
    161: "Ready",
    178: "Paused",
    179: "Charging",
    180: "Scheduled",
    189: "Ready",
    193: "Paused",
    194: "Locked",
    209: "Reserved (OCPP)",
    210: "Updating",
}
CHARGING_CODES_PULSAR_MAX: frozenset[int] = frozenset({2, 20, 21, 179})
PAUSED_CODES_PULSAR_MAX: frozenset[int] = frozenset({3, 22, 178, 193})
CAR_CONNECTED_CODES_PULSAR_MAX: frozenset[int] = frozenset(
    {1, 2, 3, 4, 5, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 178, 179, 180, 193}
)
LOCKED_CODES_PULSAR_MAX: frozenset[int] = frozenset({8, 194})

# Pulsar Plus (from jagheterfredrik/wallbox-ble api.py STATUS_CODES list)
STATUS_MAP_PULSAR_PLUS: dict[int, str] = {
    0: "Ready",
    1: "Charging",
    2: "Connected (waiting for car)",
    3: "Connected (waiting for schedule)",
    4: "Paused",
    5: "Schedule end",
    6: "Locked",
    7: "Error",
    8: "Connected (waiting for current assignation)",
    9: "Unconfigured power sharing",
    10: "Queue by power boost",
    11: "Discharging",
    12: "Connected (waiting for admin auth for MID)",
    13: "Connected (MID safety margin exceeded)",
    14: "OCPP unavailable",
    15: "OCPP charge finishing",
    16: "OCPP reserved",
    17: "Updating",
    18: "Queue by Eco Smart",
}
CHARGING_CODES_PULSAR_PLUS: frozenset[int] = frozenset({1, 11})  # 11 = discharging (V2H) also counts
PAUSED_CODES_PULSAR_PLUS: frozenset[int] = frozenset({4})
CAR_CONNECTED_CODES_PULSAR_PLUS: frozenset[int] = frozenset(
    {1, 2, 3, 4, 5, 8, 10, 11, 12, 13, 15, 18}
)
LOCKED_CODES_PULSAR_PLUS: frozenset[int] = frozenset({6})

# OCPP status (separate code space per firmware family).
OCPP_STATUS_PULSAR_MAX: dict[int, str] = {
    0: "Not available",
    1: "Not configured",
    2: "Connected",
    3: "Charging",
}
# Pulsar Plus uses a different numbering. THIS MAP IS A GUESS — only
# value 4 = "Connected" has been confirmed empirically. The others are
# extrapolated from common OCPP state names and are likely wrong; refine
# as new code values are observed in the 'OCPP status code' raw sensor.
OCPP_STATUS_PULSAR_PLUS: dict[int, str] = {
    0: "Not available",  # guess
    1: "Not configured",  # guess
    2: "Connecting",  # guess
    3: "Disconnected",  # guess
    4: "Connected",  # confirmed
    5: "Charging",  # guess
    6: "Unavailable",  # guess
}


def ocpp_status_name(code: int | None, layout_name: str | None = None) -> str | None:
    if code is None:
        return None
    m = OCPP_STATUS_PULSAR_PLUS if layout_name == "pulsar_plus" else OCPP_STATUS_PULSAR_MAX
    return m.get(code, f"Unknown ({code})")


def _maps_for(layout_name: str | None) -> tuple[dict[int, str], frozenset[int], frozenset[int], frozenset[int]]:
    """Return (status_map, charging, car_connected, locked) for a layout."""
    if layout_name == "pulsar_plus":
        return (
            STATUS_MAP_PULSAR_PLUS,
            CHARGING_CODES_PULSAR_PLUS,
            CAR_CONNECTED_CODES_PULSAR_PLUS,
            LOCKED_CODES_PULSAR_PLUS,
        )
    # Default to Pulsar MAX semantics — also reasonable for unknown layouts
    # since the MAX firmware is what botts7/esp32-wallbox targets.
    return (
        STATUS_MAP_PULSAR_MAX,
        CHARGING_CODES_PULSAR_MAX,
        CAR_CONNECTED_CODES_PULSAR_MAX,
        LOCKED_CODES_PULSAR_MAX,
    )


def status_name(code: int | None, layout_name: str | None = None) -> str:
    if code is None:
        return "Unknown"
    status_map, *_ = _maps_for(layout_name)
    return status_map.get(code, f"Unknown ({code})")


def is_charging(code: int | None, layout_name: str | None = None) -> bool:
    if code is None:
        return False
    _, charging, _, _ = _maps_for(layout_name)
    return code in charging


def is_car_connected(code: int | None, layout_name: str | None = None) -> bool:
    if code is None:
        return False
    _, _, car_connected, _ = _maps_for(layout_name)
    return code in car_connected


def is_locked_from_status(code: int | None, layout_name: str | None = None) -> bool | None:
    if code is None:
        return None
    _, _, _, locked = _maps_for(layout_name)
    return code in locked


# Backwards-compat exports (Pulsar MAX defaults) — used by older tests / call sites.
STATUS_MAP = STATUS_MAP_PULSAR_MAX
CHARGING_CODES = CHARGING_CODES_PULSAR_MAX
PAUSED_CODES = PAUSED_CODES_PULSAR_MAX
CAR_CONNECTED_CODES = CAR_CONNECTED_CODES_PULSAR_MAX


# --- Known GATT layouts ---

@dataclass(frozen=True)
class GattLayout:
    name: str
    service: str
    write_char: str
    notify_char: str
    write_without_response: bool


PULSAR_MAX_LAYOUT = GattLayout(
    name="pulsar_max",
    service="2456e1b9-26e2-8f83-e744-f34f01e9d701",
    write_char="2456e1b9-26e2-8f83-e744-f34f01e9d703",
    notify_char="2456e1b9-26e2-8f83-e744-f34f01e9d703",
    write_without_response=True,
)

PULSAR_PLUS_LAYOUT = GattLayout(
    name="pulsar_plus",
    service="331a36f5-2459-45ea-9d95-6142f0c4b307",
    write_char="a9da6040-0823-4995-94ec-9ce41ca28833",
    notify_char="a73e9a10-628f-4494-a099-12efaf72258f",
    write_without_response=False,
)

KNOWN_LAYOUTS: tuple[GattLayout, ...] = (PULSAR_MAX_LAYOUT, PULSAR_PLUS_LAYOUT)


# --- Eco Smart mode mapping (wb_mqtt.cpp:357-365) ---

ECO_MODE_OFF = 0
ECO_MODE_FULL_GREEN = 1
ECO_MODE_ECO_SMART = 2

ECO_MODE_LABELS = {
    ECO_MODE_OFF: "Off",
    ECO_MODE_FULL_GREEN: "Full Green (Solar Only)",
    ECO_MODE_ECO_SMART: "Eco Smart (Solar + Grid)",
}


# --- w_cha values ---

W_CHA_START = 1
W_CHA_STOP = 2
