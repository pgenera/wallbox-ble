# wallbox-ble

Home Assistant custom integration for controlling [Wallbox](https://wallbox.com/)
EVSE chargers locally over Bluetooth Low Energy, via a Bluetooth proxy.

Built from the open protocol used by the
[`botts7/esp32-wallbox`](https://github.com/botts7/esp32-wallbox) gateway, and
inspired by `jagheterfredrik/wallbox-ble`.

## Features

- Bluetooth auto-discovery (no manual MAC entry needed).
- Supports **multiple chargers** (one config entry per device).
- Probes both known GATT layouts on connect — works with Pulsar Plus (older
  two-characteristic layout) and Pulsar MAX (single characteristic). Custom
  UUIDs can be entered in options for unusual radios.
- Handles the optional BAPI PIN in the config flow / options + re-auth.
- Exposes the full feature surface implemented by the ESP32 reference:
  - Switches: charge, eco-smart, phase switching, dynamic power sharing, auto-lock.
  - Lock: socket lock.
  - Numbers: max charging current, eco-smart percentage, halo brightness, halo
    timeout, auto-lock timeout.
  - Selects: eco-smart mode, halo LED mode.
  - Buttons: reboot, clear all schedules.
  - Binary sensors: car connected, charging.
  - Sensors: status, per-phase current/voltage/power, charging power, session
    energy (total / green / grid / discharge), lifetime meter energy, max
    available current, OCPP status, phases, charger timezone.
  - Service: `wallbox_ble.set_schedule`, `wallbox_ble.delete_schedules`.

## Installing

Copy `custom_components/wallbox_ble/` into your Home Assistant config
directory. Restart HA. The integration should auto-discover any in-range
Wallbox chargers.

(HACS is not provided; install manually.)

## Repo layout

```
custom_components/wallbox_ble/   ← the HA integration itself
tools/wallbox_cli.py             ← standalone CLI using local Bluetooth
tools/wallbox_esphome_test.py    ← standalone CLI using an ESPHome BLE proxy
tests/test_bapi.py               ← unit tests for the wire protocol
dev/                             ← isolated HA Container dev environment
```

## Local development

```bash
# Set up a Python 3.14 venv with dev deps:
python3.14 -m venv .venv
.venv/bin/pip install -e '.[dev,esphome]'

# Run the unit tests:
.venv/bin/python -m pytest -q

# Talk to a real charger via your machine's Bluetooth adapter:
.venv/bin/python tools/wallbox_cli.py scan
.venv/bin/python tools/wallbox_cli.py info AA:BB:CC:DD:EE:FF

# Talk to a real charger via an ESPHome BLE proxy:
.venv/bin/python tools/wallbox_esphome_test.py --host 192.168.x.y --pin 1234

# Or spin up an isolated dev HA on port 8124:
cd dev && docker compose up -d
```

See `dev/README.md` for the full dev-HA workflow.

## Protocol references

- `botts7/esp32-wallbox`: the working ESPHome gateway whose BAPI implementation
  this integration mirrors.
- Frame format: `b"EaE" + length(1B or escaped) + JSON + checksum(1B)` where
  checksum is `sum(bytes) & 0xFF`.
- Responses arrive as unframed JSON chunked across BLE notifications and are
  reassembled by tracking JSON brace depth.

## Credits

The BAPI wire protocol (frame format, command opcodes, telemetry fields,
status code map) was reverse-engineered and documented by
**Daniel Botts** in [`botts7/esp32-wallbox`](https://github.com/botts7/esp32-wallbox)
(MIT License, Copyright (c) 2026 Daniel Botts). This integration is a
reimplementation of the same protocol for Home Assistant + a
Bluetooth proxy, and would not exist without that prior work.

The original Home Assistant attempt at a Pulsar Plus BLE integration by
[`jagheterfredrik/wallbox-ble`](https://github.com/jagheterfredrik/wallbox-ble)
informed the GATT layout for that model.

## License

MIT.
