"""Subscribe to coffee/shot/* and write one self-contained JSON file per shot.

Samples are buffered in memory between coffee/shot/start and coffee/shot/end
and attached to the saved file so the dashboard can replay each shot from
disk without needing a per-sample archive.

A rolling buffer of the always-on `coffee/scale/weight` stream is also kept
so each saved shot file includes the few seconds of weight readings *before*
the firmware's >1g shot detector triggered — that way replay shows the real
first drop / preinfusion plateau, matching what the live dashboard shows.

Also maintains shots/index.json (newest first) for the history view.
"""

import datetime
import glob
import json
import math
import os
import re
import sys
import time
from collections import defaultdict, deque

import paho.mqtt.client as mqtt

OUT = os.environ.get("OUT_DIR", "/data/shots")
BROKER = os.environ.get("MQTT_BROKER", "mosquitto")
PORT = int(os.environ.get("MQTT_PORT", "1883"))

T_START = "coffee/shot/start"
T_SAMPLE = "coffee/shot/sample"
T_END = "coffee/shot/end"
T_SCALE_W = "coffee/scale/weight"

INDEX = "index.json"
FILENAME_TS_RE = re.compile(r"^(\d{8}T\d{6}Z)_")

SCALE_BUFFER_SEC = 30.0
PREPEND_LOOKBACK_SEC = 5.0

# In-flight shots: shot_id -> {"start": dict|None, "samples": [dict], "prepend": [dict]}
shots_in_flight: dict[str, dict] = defaultdict(
    lambda: {"start": None, "samples": [], "prepend": []}
)

# Rolling (epoch_seconds, weight_g) tuples from coffee/scale/weight
scale_buffer: deque = deque()


def utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_ts(filename: str) -> str:
    """Best-effort wall-clock for the index from the filename prefix."""
    m = FILENAME_TS_RE.match(filename)
    if not m:
        return ""
    s = m.group(1)
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}T{s[9:11]}:{s[11:13]}:{s[13:15]}Z"


def rebuild_index() -> None:
    """Scan OUT for shot files and rewrite index.json (newest first)."""
    entries = []
    for path in sorted(glob.glob(os.path.join(OUT, "*.json")), reverse=True):
        name = os.path.basename(path)
        if name == INDEX:
            continue
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        entries.append({
            "file": name,
            "saved_at": parse_ts(name),
            "shot_id": data.get("shot_id"),
            "duration_s": data.get("duration_s"),
            "final_weight_g": data.get("final_weight_g"),
            "peak_flow_g_s": data.get("peak_flow_g_s"),
            "tank_pct_at_start": data.get("tank_pct_at_start"),
            "n_samples": len(data.get("samples", [])),
        })
    tmp = os.path.join(OUT, INDEX + ".tmp")
    with open(tmp, "w") as f:
        json.dump(entries, f, indent=2)
    os.replace(tmp, os.path.join(OUT, INDEX))


def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        print(f"connected to {BROKER}:{PORT}", flush=True)
        client.subscribe([(T_START, 0), (T_SAMPLE, 0), (T_END, 0), (T_SCALE_W, 0)])
        print(f"subscribed: {T_START}, {T_SAMPLE}, {T_END}, {T_SCALE_W}", flush=True)
    else:
        print(f"connect failed rc={rc}", flush=True)


def on_message(client, userdata, msg):
    # coffee/scale/weight is plain numeric — handle separately and bail.
    if msg.topic == T_SCALE_W:
        try:
            w = float(msg.payload)
        except ValueError:
            return
        if math.isnan(w):
            return
        now = time.time()
        scale_buffer.append((now, w))
        cutoff = now - SCALE_BUFFER_SEC
        while scale_buffer and scale_buffer[0][0] < cutoff:
            scale_buffer.popleft()
        return

    try:
        payload = json.loads(msg.payload)
    except json.JSONDecodeError as e:
        print(f"bad json on {msg.topic}: {e}", flush=True)
        return

    shot_id = str(payload.get("shot_id", "")).strip()
    if not shot_id:
        print(f"missing shot_id on {msg.topic}: {payload!r}", flush=True)
        return

    if msg.topic == T_START:
        # Capture the rolling-buffer prepend at trigger time; store with
        # negative t_ms so it slots in before the live samples.
        start_wall = time.time()
        cutoff = start_wall - PREPEND_LOOKBACK_SEC
        prepend = [
            {"t_ms": int((ts - start_wall) * 1000), "weight_g": w, "flow_g_s": 0.0}
            for ts, w in scale_buffer
            if ts >= cutoff
        ]
        shots_in_flight[shot_id] = {
            "start": payload,
            "samples": [],
            "prepend": prepend,
        }
        print(f"shot {shot_id} started ({len(prepend)} prepend samples)", flush=True)

    elif msg.topic == T_SAMPLE:
        shots_in_flight[shot_id]["samples"].append({
            "t_ms": payload.get("t_ms"),
            "weight_g": payload.get("weight_g"),
            "flow_g_s": payload.get("flow_g_s"),
        })

    elif msg.topic == T_END:
        buf = shots_in_flight.pop(
            shot_id, {"start": None, "samples": [], "prepend": []}
        )
        record = dict(payload)
        record.setdefault("samples", buf["prepend"] + buf["samples"])
        if buf["start"] and "tank_pct_at_start" not in record:
            record["tank_pct_at_start"] = buf["start"].get("tank_pct_at_start")

        ts = utc_iso()
        # Sanitise shot_id for the filename
        safe_id = re.sub(r"[^A-Za-z0-9_-]+", "_", shot_id)
        path = os.path.join(OUT, f"{ts}_{safe_id}.json")
        with open(path, "w") as f:
            json.dump(record, f, indent=2)
        print(
            f"saved {path} ({len(record.get('samples', []))} samples, "
            f"{record.get('final_weight_g')}g in {record.get('duration_s')}s)",
            flush=True,
        )
        rebuild_index()


def main():
    os.makedirs(OUT, exist_ok=True)
    rebuild_index()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="shot-logger")
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(BROKER, PORT, keepalive=60)
    client.loop_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
