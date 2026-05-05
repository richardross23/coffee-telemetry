# coffee-telemetry

A small home-server stack that captures every espresso shot from a
Bluetooth scale + accelerometer-instrumented machine and renders it on a
live dashboard. Three containers, one MQTT topic tree, one self-contained
JSON file per shot.

## What it is

```
                     ┌──────────────────┐
                     │  ESP32 firmware  │
                     │  (water-meter)   │
                     │                  │
                     │ • LIS3DH/QMI8658 │  pump on/off detection
                     │ • Acaia BLE      │  weight stream
                     │ • VL53L1X        │  water-tank level
                     └────────┬─────────┘
                              │ MQTT (TCP 1883)
                              ▼
                  ┌───────────────────────┐
                  │       Mosquitto       │
                  └───┬───────────────┬───┘
              TCP 1883│         WS 9001│
                      ▼                ▼
            ┌──────────────┐    ┌──────────────┐
            │  shot-logger │    │   browser    │
            │              │    │  dashboard   │
            │  one JSON    │    │              │
            │  file/shot + │    │  live curve  │
            │  index       │    │  + history   │
            └──────┬───────┘    └──────▲───────┘
                   │ writes             │ reads
                   ▼                    │
                ./shots/ ───────────────┘
                                 (read-only, served by Caddy)
```

The ESP32 firmware lives in a separate repo:
[richardross23/water-meter](https://github.com/richardross23/water-meter).
The MQTT contract is documented there in
[`docs/mqtt-architecture.md`](https://github.com/richardross23/water-meter/blob/main/docs/mqtt-architecture.md).

## What's in here

- **`mosquitto/`** — broker config (anonymous LAN-only)
- **`logger/`** — Python service that subscribes to `coffee/shot/*`, buffers
  samples per shot, and writes one self-contained JSON file per shot
- **`web/`** — single-file dashboard (HTML/CSS/JS), MQTT-over-WebSockets
  client, live curve + history replay + Decent `.shot` export
- **`tools/`** — utilities that aren't part of the running stack (see
  [`tools/README.md`](tools/README.md))

## Quick start

Requires Docker (any flavour — Docker Desktop, OrbStack, Linux Docker,
Podman with compose, etc).

```sh
git clone https://github.com/richardross23/coffee-telemetry
cd coffee-telemetry
docker compose up -d
```

Then:

| Endpoint | What |
|---|---|
| `http://<host>:8418` | Dashboard |
| `<host>:1883` | MQTT broker (point the firmware here) |
| `./shots/*.json` | Captured shots (one file each) |

Replace `<host>` with the LAN address of the machine running the stack.
On macOS: `ipconfig getifaddr en0`.

## Pointing the firmware at this broker

In your `firmware/coffee-tank.yaml` (water-meter repo):

```yaml
mqtt:
  broker: <host>
  port: 1883
  discovery: false
  client_id: coffee-tank
```

Re-flash. Full firmware patch:
[`docs/mqtt-firmware-patch.md`](https://github.com/richardross23/water-meter/blob/main/docs/mqtt-firmware-patch.md).

## Smoke test (no firmware needed)

```sh
# Subscribe in one shell:
docker exec -it mosquitto mosquitto_sub -h localhost -t test

# Publish in another:
docker exec -it mosquitto mosquitto_pub -h localhost -t test -m hello
```

To exercise the dashboard end-to-end, replay a synthetic shot:

```sh
docker exec -i mosquitto mosquitto_pub -h localhost -t coffee/shot/start \
  -m '{"shot_id":"demo","tank_pct_at_start":73}'

for t in 0 500 1000 1500 2000 2500 3000; do
  docker exec -i mosquitto mosquitto_pub -h localhost -t coffee/shot/sample \
    -m "{\"shot_id\":\"demo\",\"t_ms\":$t,\"weight_g\":$(echo "$t/200" | bc -l)}"
  sleep 0.2
done

docker exec -i mosquitto mosquitto_pub -h localhost -t coffee/shot/end \
  -m '{"shot_id":"demo","duration_s":3.0,"final_weight_g":15.0,"pump_off_at_ms":3000}'
```

Open the dashboard — the shot lands in History within a second.

## Operations

```sh
docker compose ps                     # status
docker compose logs -f shot-logger    # one line per saved shot
docker compose logs -f mosquitto      # broker logs
docker compose restart shot-logger    # reload after editing logger.py
docker compose down                   # stop everything (data persists)
```

## Notes

- **No auth.** Broker is wide open on the LAN. Don't forward `1883`/`9001`
  past the router. To lock it down: generate a `mosquitto_passwd` file,
  mount it, flip `allow_anonymous false` in `mosquitto.conf`.
- **Persistence.** Shot files live in `./shots/`, broker session state in
  `./mosquitto/data/` — both bind-mounted, both survive `docker compose down`.
- **Index rebuild.** The logger keeps `shots/index.json` in sync
  incrementally; on startup it scans the directory once. Safe to delete the
  index — it'll regenerate on next restart.
- **`coffee/water/pct` and `coffee/device/state` are retained** by the
  broker, so the tank widget and offline badge populate immediately.

## License

MIT — see [`LICENSE`](LICENSE).
