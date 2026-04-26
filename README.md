# coffee-telemetry

Home-server side of the espresso shot logger. Runs:

- **Mosquitto** — MQTT broker on `1883` (TCP) and `9001` (WebSockets)
- **shot-logger** — Python receiver that buffers `coffee/shot/sample` between
  start and end, writes one self-contained JSON file per shot to `./shots/`,
  and maintains `shots/index.json` for the history view
- **web** — Caddy serving a Plotly dashboard on `8418` with a **Live** tab
  (live curve + numeric weight/elapsed/flow overlay + state badge + tank %)
  and a **History** tab (click any past shot to replay on the same chart).
  `./shots/` is mounted read-only into Caddy at `/shots/`.

The ESP32 firmware that publishes to this broker lives in
[richardross23/water-meter](https://github.com/richardross23/water-meter).
The data model is documented there in
[`docs/mqtt-architecture.md`](https://github.com/richardross23/water-meter/blob/main/docs/mqtt-architecture.md).

## Quick start

Requires Docker (OrbStack or Docker Desktop).

```sh
git clone <this-repo> coffee-telemetry
cd coffee-telemetry
docker compose up -d
```

Then:

- Browser: <http://192.168.1.242:8418> — live shot dashboard
- MQTT broker: `192.168.1.242:1883` — point the firmware here
- Saved shots: `./shots/*.json`

Substitute the Mac's LAN IP if it changes.

## Pointing the firmware at this broker

In `firmware/coffee-tank.yaml` (in the water-meter repo) add:

```yaml
mqtt:
  broker: 192.168.1.242
  port: 1883
  discovery: false
  client_id: coffee-tank
```

…then re-flash. See the firmware repo's
[`docs/mqtt-firmware-patch.md`](https://github.com/richardross23/water-meter/blob/main/docs/mqtt-firmware-patch.md)
for the full patch.

## Smoke test

With the stack up:

```sh
# in one terminal — subscribe
docker exec -it mosquitto mosquitto_sub -h localhost -t test

# in another — publish
docker exec -it mosquitto mosquitto_pub -h localhost -t test -m hello
```

The subscriber should print `hello`.

To exercise the dashboard without the firmware connected, replay a saved shot:

```sh
docker exec -i mosquitto mosquitto_pub -h localhost -t coffee/shot/start \
  -m '{"shot_id":"demo","tank_pct_at_start":73,"device":"coffee-tank"}'

for t in 0 500 1000 1500 2000 2500 3000; do
  docker exec -i mosquitto mosquitto_pub -h localhost -t coffee/shot/sample \
    -m "{\"shot_id\":\"demo\",\"t_ms\":$t,\"weight_g\":$(echo "$t/200" | bc -l),\"flow_g_s\":1.5}"
  sleep 0.2
done
```

## Layout

```
docker-compose.yml
mosquitto/
  config/mosquitto.conf       # broker config — anonymous LAN access
  data/                       # persistence (auto-populated)
  log/                        # broker logs
logger/
  logger.py                   # subscribes to coffee/shot/{start,sample,end},
                              # buffers samples, writes self-contained JSON
                              # + maintains index.json
web/
  Caddyfile
  index.html                  # Plotly dashboard + history/replay,
                              # paho-mqtt over WS
shots/                        # captured shots, one JSON file each
  index.json                  # newest-first listing for the history view
```

## Operations

```sh
docker compose ps                     # status
docker compose logs -f mosquitto      # broker logs
docker compose logs -f shot-logger    # logger output (one line per saved shot)
docker compose restart shot-logger    # after editing logger/logger.py
docker compose down                   # stop everything (data persists)
```

## Notes

- **No auth.** Broker is open to the LAN. Don't forward `1883`/`9001` past
  the router. To add auth, generate a `mosquitto_passwd` file, mount it,
  and flip `allow_anonymous false` in `mosquitto.conf`.
- **Persistence** lives in `./mosquitto/data/` and `./shots/` — both are
  bind-mounted, so the data survives `docker compose down`.
- **Live graph only redraws on `coffee/shot/start`** — open the page mid-shot
  and you'll see nothing until the next shot begins. Use the **History** tab
  to replay any saved shot on the same chart (samples are stored alongside
  the summary so replay is offline).
- **Old shot files have no `samples` array** — anything saved before the
  buffering logger was added will replay as an empty curve with metadata only.
  New shots are fully replayable.
- **`coffee/water/pct` and `coffee/device/state` are retained** — the tank
  widget and the offline badge populate immediately on page load.
