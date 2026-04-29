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
T_META_SET = "coffee/shot/metadata/set"
T_DELETE = "coffee/shot/delete"

# Whitelist of fields the dashboard is allowed to set on a shot via MQTT.
# Anything else in the payload is ignored.
META_ALLOWED_FIELDS = frozenset({
    "bean_brand", "bean_type", "roast_date", "roast_level",
    "bean_weight_g",
    "grinder_model", "grinder_setting",
    # Grinder log import (tools/import_grinder_log.py): the actual ground
    # weight is more accurate than the user's manual bean_weight_g entry.
    "ground_weight_g", "grind_duration_s", "grinder_recipe",
    "notes",
})

# Fields that should NEVER auto-inherit from the previous shot. Bean /
# grinder / dose carry forward (rarely change between back-to-back shots);
# notes are per-shot commentary; ground_weight / grind_duration / recipe
# are unique per grinder event and don't make sense to reuse.
META_INHERIT_EXCLUDE = frozenset({
    "notes",
    "ground_weight_g", "grind_duration_s", "grinder_recipe",
})

INDEX = "index.json"
FILENAME_TS_RE = re.compile(r"^(\d{8}T\d{6}Z)_")

SCALE_BUFFER_SEC = 30.0
PREPEND_LOOKBACK_SEC = 10.0

# Tare-event filter — the firmware fires shot/start on weight delta,
# so taring the scale (or fiddling with the cup) currently produces
# bogus "shots" that return to zero with wild excursions in the curve.
# Drop them at save time rather than poison the history view.
MIN_FINAL_WEIGHT_G = 2.0
WEIGHT_RANGE_G = (-5.0, 200.0)


def looks_like_tare(record: dict) -> tuple[bool, str]:
    """Return (is_tare, reason) for a shot record about to be saved."""
    samples = record.get("samples") or []
    if not samples:
        return True, "no samples"
    final = record.get("final_weight_g") or 0
    if abs(final) < MIN_FINAL_WEIGHT_G:
        return True, f"final_weight_g={final:.2f}g (below {MIN_FINAL_WEIGHT_G}g)"
    lo, hi = WEIGHT_RANGE_G
    for s in samples:
        w = s.get("weight_g")
        if w is None:
            continue
        if w < lo or w > hi:
            return True, f"weight {w:.1f}g outside [{lo}, {hi}]g"
    return False, ""

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


def latest_metadata() -> dict:
    """Read the most recent saved shot's metadata for auto-inheriting onto
    the next shot. Excludes META_INHERIT_EXCLUDE (notes etc.) so per-shot
    commentary doesn't get reused. Returns an empty dict if no prior
    shots exist or none have inheritable metadata."""
    paths = sorted(glob.glob(os.path.join(OUT, "*.json")), reverse=True)
    for path in paths:
        if os.path.basename(path) == INDEX:
            continue
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        meta = data.get("metadata") or {}
        inheritable = {
            k: v for k, v in meta.items()
            if k in META_ALLOWED_FIELDS and k not in META_INHERIT_EXCLUDE
        }
        if inheritable:
            return inheritable
    return {}


def first_drop_t_ms(samples: list[dict], threshold: float = 0.1) -> int | None:
    """First t_ms where weight crosses threshold for two consecutive samples."""
    consecutive = 0
    for i, s in enumerate(samples):
        w = s.get("weight_g") or 0
        if w >= threshold:
            consecutive += 1
            if consecutive >= 2:
                return samples[i - 1].get("t_ms")
        else:
            consecutive = 0
    return None


def derive_metadata_summary(meta: dict, saved_at_iso: str | None, final_weight_g: float | None) -> dict:
    """Build the convenience fields the history list shows at a glance.

    - brew_ratio_str: '1:2.27' if both dose and yield present
    - days_off_roast: int days between roast_date and saved_at
    - bean_label: 'Industry Beans · Yirgacheffe' (or whichever side is filled)
    """
    out = {}
    # Ground weight from the grinder is more accurate than the manual dose
    # entry — use it for brew_ratio when present.
    dose = meta.get("ground_weight_g") or meta.get("bean_weight_g")
    if dose and final_weight_g and dose > 0:
        out["brew_ratio_str"] = f"1:{(final_weight_g / dose):.2f}"

    roast = meta.get("roast_date")
    if roast and saved_at_iso:
        try:
            roast_d = datetime.date.fromisoformat(roast)
            saved_d = datetime.datetime.fromisoformat(
                saved_at_iso.replace("Z", "+00:00")
            ).date()
            out["days_off_roast"] = (saved_d - roast_d).days
        except (ValueError, TypeError):
            pass

    parts = [meta.get("bean_brand"), meta.get("bean_type")]
    label = " · ".join(p for p in parts if p)
    if label:
        out["bean_label"] = label

    return out


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
        samples = data.get("samples") or []
        first_t = first_drop_t_ms(samples)
        last_t = samples[-1].get("t_ms") if samples else None
        pour_time_s = (
            (last_t - first_t) / 1000.0
            if first_t is not None and last_t is not None
            else None
        )
        meta = data.get("metadata") or {}
        saved_at = parse_ts(name)
        entry = {
            "file": name,
            "saved_at": saved_at,
            "shot_id": data.get("shot_id"),
            "duration_s": data.get("duration_s"),
            "pour_time_s": pour_time_s,
            "final_weight_g": data.get("final_weight_g"),
            "peak_flow_g_s": data.get("peak_flow_g_s"),
            "tank_pct_at_start": data.get("tank_pct_at_start"),
            "n_samples": len(samples),
            "metadata": meta,
        }
        entry.update(derive_metadata_summary(meta, saved_at, data.get("final_weight_g")))
        entries.append(entry)
    tmp = os.path.join(OUT, INDEX + ".tmp")
    with open(tmp, "w") as f:
        json.dump(entries, f, indent=2)
    os.replace(tmp, os.path.join(OUT, INDEX))


def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        print(f"connected to {BROKER}:{PORT}", flush=True)
        client.subscribe([
            (T_START, 0), (T_SAMPLE, 0), (T_END, 0),
            (T_SCALE_W, 0), (T_META_SET, 0), (T_DELETE, 0),
        ])
        print(
            f"subscribed: {T_START}, {T_SAMPLE}, {T_END}, "
            f"{T_SCALE_W}, {T_META_SET}, {T_DELETE}",
            flush=True,
        )
    else:
        print(f"connect failed rc={rc}", flush=True)


def delete_shot(file_basename: str) -> tuple[bool, str]:
    """Remove a saved shot file. Validates against path traversal and the
    reserved index.json filename."""
    safe_name = os.path.basename(file_basename)
    if not safe_name or safe_name == INDEX:
        return False, "invalid filename"
    path = os.path.join(OUT, safe_name)
    if not os.path.isfile(path):
        return False, f"not found: {safe_name}"
    try:
        os.remove(path)
    except OSError as e:
        return False, f"remove failed: {e}"
    return True, f"deleted {safe_name}"


def apply_metadata(file_basename: str, raw_metadata: dict) -> tuple[bool, str]:
    """Open the named shot file, merge whitelisted metadata, write back atomically."""
    cleaned = {k: v for k, v in raw_metadata.items() if k in META_ALLOWED_FIELDS}
    if not cleaned:
        return False, "no allowed fields in payload"

    # Keep only this dir — guard against path traversal.
    safe_name = os.path.basename(file_basename)
    path = os.path.join(OUT, safe_name)
    if not os.path.isfile(path):
        return False, f"file not found: {safe_name}"

    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return False, f"read failed: {e}"

    existing = data.get("metadata") or {}
    existing.update(cleaned)
    data["metadata"] = existing

    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    return True, f"updated {len(cleaned)} field(s)"


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

    # Metadata edits (bean / grinder / dose / notes) come from the dashboard.
    if msg.topic == T_META_SET:
        file_name = payload.get("file")
        meta = payload.get("metadata") or {}
        if not file_name or not isinstance(meta, dict):
            print(f"bad metadata payload: {payload!r}", flush=True)
            return
        ok, info = apply_metadata(file_name, meta)
        print(f"metadata for {file_name}: {info}", flush=True)
        if ok:
            rebuild_index()
        return

    # Shot deletion. Browser confirms with the user before publishing.
    if msg.topic == T_DELETE:
        file_name = payload.get("file")
        if not file_name:
            print(f"bad delete payload: {payload!r}", flush=True)
            return
        ok, info = delete_shot(file_name)
        print(f"delete: {info}", flush=True)
        if ok:
            rebuild_index()
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

        is_tare, reason = looks_like_tare(record)
        if is_tare:
            print(f"dropped shot {shot_id}: {reason}", flush=True)
            return

        # Auto-inherit bean/grinder/dose/notes from the previous shot — most
        # users keep the same setup for runs of shots and only change one
        # variable at a time, so carrying it forward saves a click per shot.
        # The dashboard's edit modal still lets them override per-shot.
        inherited = latest_metadata()
        if inherited:
            record.setdefault("metadata", inherited)

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
