# Dev Home Assistant container

A throwaway Home Assistant instance for testing the `wallbox_ble` integration
in isolation from your production HA.

## What you get

- Home Assistant Container, pinned-by-tag (`stable`).
- Listens on **http://localhost:8124** — different port from a typical
  production HA (8123) so the two can coexist on the same host.
- The custom component is bind-mounted from `../custom_components/`, so editing
  source on the host + restarting the container is the dev loop.
- Throwaway config in `./config/`. Everything except `configuration.yaml`
  (databases, `.storage/`, logs) is gitignored.

## First-time setup

```bash
cd dev
docker compose up -d
# Wait ~30s for HA to bootstrap, then:
open http://localhost:8124          # macOS
xdg-open http://localhost:8124      # Linux
```

Onboard a throwaway local-only account.

## Wiring up the test ESPHome BLE proxy

The container has **no local Bluetooth adapter** — it talks to your chargers
through an ESPHome device with `bluetooth_proxy:` enabled.

1. Make sure the test ESPHome proxy is reachable from this host on TCP 6053.
2. In dev HA: **Settings → Devices & Services → Add Integration → ESPHome →**
   enter the proxy's host/IP, password (or PSK if it uses noise encryption).
3. The `wallbox_ble` integration should auto-discover the charger within a
   minute (look for a notification in the sidebar). If it does not, add it
   manually via **Add Integration → Wallbox BLE**.

> ⚠️  Do **not** point this dev HA at the same ESPHome proxy your production
> HA is using. ESPHome BLE proxies effectively serve only one API client's
> scanner subscription at a time, and both HAs will fight for the BLE
> connection slots. Use a separate ESP32 with the
> [`bluetooth_proxy`](https://esphome.io/projects/index.html#bluetooth-proxy)
> firmware for development.

## Iteration loop

```bash
# Edit files under ../custom_components/wallbox_ble/, then:
docker compose restart
```

Logs:

```bash
docker compose logs -f homeassistant | grep wallbox_ble
```

## Reset

```bash
docker compose down -v
rm -rf config/.storage config/*.db* config/home-assistant.log*
```

This wipes the dev HA state but keeps `configuration.yaml` so you can start
fresh without re-onboarding the container itself.

## Not using Docker?

The protocol layer is also exercisable without Home Assistant:

```bash
# Live test through an ESPHome BLE proxy (recommended):
.venv/bin/python tools/wallbox_esphome_test.py --host <proxy-host>

# Live test using the host's own Bluetooth adapter:
.venv/bin/python tools/wallbox_cli.py scan
.venv/bin/python tools/wallbox_cli.py info <mac>
```
