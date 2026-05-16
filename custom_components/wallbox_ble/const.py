"""Constants for the Wallbox BLE integration."""

from __future__ import annotations

DOMAIN = "wallbox_ble"

# Config / options keys
CONF_PIN = "pin"
CONF_SERVICE_UUID = "service_uuid"
CONF_WRITE_CHAR_UUID = "write_char_uuid"
CONF_NOTIFY_CHAR_UUID = "notify_char_uuid"
CONF_FAST_POLL = "fast_poll_seconds"
CONF_SLOW_POLL = "slow_poll_seconds"

DEFAULT_FAST_POLL = 10
DEFAULT_SLOW_POLL = 30
SLOW_EVERY_N_FAST = 3  # 30s when fast=10s
PING_INTERVAL_S = 30

# Discovery name patterns (regex applied case-insensitively in config_flow)
NAME_PATTERNS = (r"^WB\d", r"wallbox")

MANUFACTURER = "Wallbox"
