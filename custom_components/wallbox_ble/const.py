"""Constants for the Wallbox BLE integration."""

from __future__ import annotations

DOMAIN = "wallbox_ble"

# Config / options keys
CONF_PIN = "pin"
CONF_SERVICE_UUID = "service_uuid"
CONF_WRITE_CHAR_UUID = "write_char_uuid"
CONF_NOTIFY_CHAR_UUID = "notify_char_uuid"
CONF_INTERVAL_CHARGING = "interval_charging"
CONF_INTERVAL_CONNECTED = "interval_connected"
CONF_INTERVAL_IDLE = "interval_idle"
# Legacy keys still read for backwards compatibility with existing entries.
CONF_FAST_POLL = "fast_poll_seconds"
CONF_SLOW_POLL = "slow_poll_seconds"

# Per-state poll cadence (seconds). r_dat happens on every wake. Everything
# else is gated further:
#   * r_dca: only while charging
#   * r_sta: every R_STA_INTERVAL_S
#   * settings (g_alo, g_ecos, g_psh, g_phsw, g_tzn, g_halocfg): once at
#     startup, then only after a write triggers a readback
DEFAULT_INTERVAL_CHARGING = 10
DEFAULT_INTERVAL_CONNECTED = 30
DEFAULT_INTERVAL_IDLE = 30  # quick enough to notice a plug-in
R_STA_INTERVAL_S = 300
PING_INTERVAL_S = 30

# Discovery name patterns (regex applied case-insensitively in config_flow)
NAME_PATTERNS = (r"^WB\d", r"wallbox")

MANUFACTURER = "Wallbox"
