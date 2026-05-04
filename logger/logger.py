"""MQTT subscriber that writes one self-contained JSON file per espresso shot
and maintains an index for the dashboard's history view.

Subscribes to coffee/shot/{start,sample,end} for the shot itself, plus
coffee/scale/weight for a rolling pre-shot buffer that gets prepended to
each saved file (so replay shows the same scale lead-in the live dashboard
shows). Also handles coffee/shot/metadata/set and coffee/shot/delete edits.

Reassembles coffee/shot/raw chunks into shots/_raw_archive/<id>.json as
belt-and-braces backup of the device's own canonical raw archive. Never
read by the dashboard; safe to delete the directory once device-side
retrieval is trusted.
"""

import datetime
import glob
import json
import logging
import math
import os
import re
import sys
import time
from collections import defaultdict, deque

import paho.mqtt.client as mqtt

log = logging.getLogger("shot-logger")

OUT = os.environ.get("OUT_DIR", "/data/shots")
BROKER = os.environ.get("MQTT_BROKER", "mosquitto")
PORT = int(os.environ.get("MQTT_PORT", "1883"))

T_START = "coffee/shot/start"
T_SAMPLE = "coffee/shot/sample"
T_END = "coffee/shot/end"
T_RAW = "coffee/shot/raw"
T_SCALE_W = "coffee/scale/weight"
T_META_SET = "coffee/shot/metadata/set"
T_DELETE = "coffee/shot/delete"

# Whitelist of metadata fields the dashboard may set on a saved shot.
META_ALLOWED_FIELDS = frozenset({
    "bean_brand", "bean_type", "roast_date", "roast_level",
    "bean_weight_g",
    "grinder_model", "grinder_setting",
    "ground_weight_g", "grind_duration_s", "grinder_recipe",
    "notes",
    "enjoyment",
})

# Fields that don't auto-inherit from the previous shot (per-shot only).
META_INHERIT_EXCLUDE = frozenset({
    "notes",
    "ground_weight_g", "grind_duration_s", "grinder_recipe",
    "enjoyment",
})

INDEX = "index.json"
FILENAME_TS_RE = re.compile(r"^(\d{8}T\d{6}Z)_")

SCALE_BUFFER_SEC = 30.0
PREPEND_LOOKBACK_SEC = 10.0
# Anything heavier than this in the prepend window is the user placing
# a cup, not a real first drop — drop it so it doesn't pollute the curve.
PREPEND_MAX_WEIGHT_G = 5.0

# Tare-event filter: the firmware can fire shot/start on weight delta
# during a tare, producing zero-weight "shots". Drop them at save time.
MIN_FINAL_WEIGHT_G = 2.0
WEIGHT_RANGE_G = (-5.0, 200.0)


def utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_ts(filename: str) -> str:
    """Wall-clock ISO string from the filename's timestamp prefix, or ''."""
    m = FILENAME_TS_RE.match(filename)
    if not m:
        return ""
    s = m.group(1)
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}T{s[9:11]}:{s[11:13]}:{s[13:15]}Z"


def looks_like_tare(record: dict) -> tuple[bool, str]:
    """True if this record looks like a tare event masquerading as a shot.

    shot/end's final_weight_g is a snapshot — if the user lifted the cup
    or BLE went stale, it can read zero on a real shot. We use the max
    sample weight (the same stream the dashboard rendered live) as ground
    truth.
    """
    samples = record.get("samples") or []
    if not samples:
        return True, "no samples"
    weights = [s.get("weight_g") for s in samples if s.get("weight_g") is not None]
    sample_max = max(weights) if weights else 0
    if sample_max < MIN_FINAL_WEIGHT_G:
        return True, f"max sample {sample_max:.2f}g < {MIN_FINAL_WEIGHT_G}g"
    lo, hi = WEIGHT_RANGE_G
    for w in weights:
        if w < lo or w > hi:
            return True, f"weight {w:.1f}g outside [{lo}, {hi}]g"
    return False, ""


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


def derive_metadata_summary(meta: dict, saved_at_iso: str | None,
                             final_weight_g: float | None) -> dict:
    """Convenience fields the history list shows at a glance."""
    out = {}
    # Grinder-reported ground weight is more accurate than the manual entry.
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

    label = " · ".join(p for p in (meta.get("bean_brand"), meta.get("bean_type")) if p)
    if label:
        out["bean_label"] = label
    return out


def build_index_entry(name: str, data: dict) -> dict:
    """Compute one history-index row from a shot file's contents."""
    samples = data.get("samples") or []
    first_t = first_drop_t_ms(samples)
    last_t = samples[-1].get("t_ms") if samples else None

    # Pour time prefers Acaia's auto-stop event over our last-sample fallback;
    # Acaia knows the difference between a finished shot and dribbles.
    acaia_stop_s = data.get("acaia_stop_at_s")
    if acaia_stop_s is not None and first_t is not None:
        pour_time_s = acaia_stop_s - first_t / 1000.0
    elif first_t is not None and last_t is not None:
        pour_time_s = (last_t - first_t) / 1000.0
    else:
        pour_time_s = None

    pump_off_s = data.get("pump_off_at_s")
    last_drop_s = data.get("last_drop_at_s")
    after_drip_s = (
        max(0.0, last_drop_s - pump_off_s)
        if pump_off_s is not None and last_drop_s is not None
        else None
    )

    saved_at = parse_ts(name)
    meta = data.get("metadata") or {}
    entry = {
        "file": name,
        "saved_at": saved_at,
        "shot_id": data.get("shot_id"),
        "duration_s": data.get("duration_s"),
        "pour_time_s": pour_time_s,
        "pump_off_at_s": pump_off_s,
        "last_drop_at_s": last_drop_s,
        "pump_time_s": pump_off_s,
        "after_drip_s": after_drip_s,
        "dwell_s": data.get("dwell_s"),
        "acaia_start_at_s": data.get("acaia_start_at_s"),
        "acaia_stop_at_s": data.get("acaia_stop_at_s"),
        "acaia_stop_weight_g": data.get("acaia_stop_weight_g"),
        "final_weight_g": data.get("final_weight_g"),
        "peak_flow_g_s": data.get("peak_flow_g_s"),
        "tank_pct_at_start": data.get("tank_pct_at_start"),
        "n_samples": len(samples),
        "metadata": meta,
    }
    entry.update(derive_metadata_summary(meta, saved_at, data.get("final_weight_g")))
    return entry


# --- Index cache --------------------------------------------------------------
# Kept in memory to avoid re-scanning every shot file from disk on every
# message. Updated incrementally on save / metadata-edit / delete.

_index: dict[str, dict] = {}


def _read_shot(path: str) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.warning("skipping %s: %s", os.path.basename(path), e)
        return None


def index_scan() -> None:
    """One-time disk scan at startup."""
    _index.clear()
    for path in glob.glob(os.path.join(OUT, "*.json")):
        name = os.path.basename(path)
        if name == INDEX:
            continue
        data = _read_shot(path)
        if data is not None:
            _index[name] = build_index_entry(name, data)
    write_index()


def update_index(name: str) -> None:
    """Recompute one entry from disk and rewrite the index file."""
    path = os.path.join(OUT, name)
    if not os.path.isfile(path):
        _index.pop(name, None)
    else:
        data = _read_shot(path)
        if data is not None:
            _index[name] = build_index_entry(name, data)
    write_index()


def remove_from_index(name: str) -> None:
    _index.pop(name, None)
    write_index()


def write_index() -> None:
    """Atomically write the index sorted by filename desc (newest first)."""
    entries = [_index[k] for k in sorted(_index, reverse=True)]
    tmp = os.path.join(OUT, INDEX + ".tmp")
    with open(tmp, "w") as f:
        json.dump(entries, f, indent=2)
    os.replace(tmp, os.path.join(OUT, INDEX))


def latest_metadata() -> dict:
    """Inheritable metadata from the most recent saved shot, or {}."""
    for name in sorted(_index, reverse=True):
        meta = _index[name].get("metadata") or {}
        inheritable = {
            k: v for k, v in meta.items()
            if k in META_ALLOWED_FIELDS and k not in META_INHERIT_EXCLUDE
        }
        if inheritable:
            return inheritable
    return {}


# --- In-flight shot state -----------------------------------------------------

shots_in_flight: dict[str, dict] = defaultdict(
    lambda: {"start": None, "samples": [], "prepend": []}
)
scale_buffer: deque = deque()


# --- MQTT handlers ------------------------------------------------------------

def on_connect(client, userdata, flags, rc, properties=None):
    if rc != 0:
        log.error("connect failed rc=%s", rc)
        return
    log.info("connected to %s:%s", BROKER, PORT)
    client.subscribe([
        (T_START, 0), (T_SAMPLE, 0), (T_END, 0), (T_RAW, 0),
        (T_SCALE_W, 0), (T_META_SET, 0), (T_DELETE, 0),
    ])


def delete_shot(file_basename: str) -> tuple[bool, str]:
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
    """Merge whitelisted metadata into a saved shot file (atomic write)."""
    cleaned = {k: v for k, v in raw_metadata.items() if k in META_ALLOWED_FIELDS}
    if not cleaned:
        return False, "no allowed fields in payload"

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


def handle_scale_weight(payload: bytes) -> None:
    try:
        w = float(payload)
    except ValueError:
        return
    if math.isnan(w):
        return
    now = time.time()
    scale_buffer.append((now, w))
    cutoff = now - SCALE_BUFFER_SEC
    while scale_buffer and scale_buffer[0][0] < cutoff:
        scale_buffer.popleft()


def handle_shot_start(shot_id: str, payload: dict) -> None:
    start_wall = time.time()
    cutoff = start_wall - PREPEND_LOOKBACK_SEC
    prepend = [
        {"t_ms": int((ts - start_wall) * 1000), "weight_g": w, "flow_g_s": 0.0}
        for ts, w in scale_buffer
        if ts >= cutoff and abs(w) <= PREPEND_MAX_WEIGHT_G
    ]
    shots_in_flight[shot_id] = {
        "start": payload,
        "samples": [],
        "prepend": prepend,
    }
    log.info("shot %s started (%d prepend samples)", shot_id, len(prepend))


def handle_shot_sample(shot_id: str, payload: dict) -> None:
    shots_in_flight[shot_id]["samples"].append({
        "t_ms": payload.get("t_ms"),
        "weight_g": payload.get("weight_g"),
        "flow_g_s": payload.get("flow_g_s"),
    })


# Firmware fields we convert from ms → seconds when present.
_MS_FIELDS = (
    ("acaia_start_at_ms", "acaia_start_at_s"),
    ("acaia_stop_at_ms", "acaia_stop_at_s"),
    ("pump_off_at_ms", "pump_off_at_s"),
    ("last_drop_at_ms", "last_drop_at_s"),
)


# --- Raw-capture archive -----------------------------------------------------
# Firmware publishes coffee/shot/raw as `<shot_id>|<idx>|<total>|<bytes>`
# chunks (~3.5 KB each); concatenated payloads JSON-decode to the full
# accel + weight + events trace. The device also stores these to its own
# flash and serves them at /shots/<id>.json — the device is canonical;
# this MQTT path is belt-and-braces backup. Reassembled JSON lands in
# shots/_raw_archive/<id>.json and is never read by the dashboard. Safe
# to delete the directory entirely once device-side retrieval is trusted.

RAW_ARCHIVE_DIR = "_raw_archive"
RAW_TTL_SEC = 120.0
_raw_chunks: dict[str, dict] = {}    # shot_id -> {chunks: {idx: bytes}, total: int, ts: float}


def handle_raw_chunk(payload: bytes) -> None:
    parts = payload.split(b"|", 3)
    if len(parts) != 4:
        log.warning("raw: malformed chunk %r", payload[:80])
        return
    try:
        shot_id = parts[0].decode("ascii")
        idx = int(parts[1])
        total = int(parts[2])
    except (ValueError, UnicodeDecodeError):
        log.warning("raw: bad header %r", payload[:80])
        return

    bucket = _raw_chunks.setdefault(
        shot_id, {"chunks": {}, "total": total, "ts": time.time()}
    )
    bucket["chunks"][idx] = parts[3]
    bucket["total"] = total
    _evict_stale_raw()

    if len(bucket["chunks"]) < total:
        return

    del _raw_chunks[shot_id]
    body = b"".join(bucket["chunks"][i] for i in range(total))
    try:
        raw = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        log.warning("raw %s: parse failed (%d bytes): %s", shot_id, len(body), e)
        return
    _save_raw_archive(shot_id, raw)


def _save_raw_archive(shot_id: str, raw: dict) -> None:
    raw_dir = os.path.join(OUT, RAW_ARCHIVE_DIR)
    os.makedirs(raw_dir, exist_ok=True)
    path = os.path.join(raw_dir, f"{shot_id}.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(raw, f, indent=2)
    os.replace(tmp, path)
    log.info("raw %s archived (%d bytes)", shot_id, os.path.getsize(path))


def _evict_stale_raw() -> None:
    cutoff = time.time() - RAW_TTL_SEC
    for sid in [k for k, v in _raw_chunks.items() if v["ts"] < cutoff]:
        log.warning("raw %s: incomplete after %.0fs, dropping", sid, RAW_TTL_SEC)
        del _raw_chunks[sid]


def handle_shot_end(shot_id: str, payload: dict) -> None:
    # Firmware aborts (backflush, blind basket, descale, accidental
    # lever-bump): drop without running our heuristic tare filter.
    if payload.get("aborted"):
        shots_in_flight.pop(shot_id, None)
        log.info(
            "dropped shot %s: aborted by firmware (reason=%s, duration=%ss, weight=%sg)",
            shot_id,
            payload.get("reason", "?"),
            payload.get("duration_s"),
            payload.get("final_weight_g"),
        )
        return

    buf = shots_in_flight.pop(shot_id, None) or {"start": None, "samples": [], "prepend": []}
    record = dict(payload)
    record.setdefault("samples", buf["prepend"] + buf["samples"])
    if buf["start"] and "tank_pct_at_start" not in record:
        record["tank_pct_at_start"] = buf["start"].get("tank_pct_at_start")

    dwell_ms = payload.get("dwell_ms")
    if dwell_ms:
        record["dwell_s"] = dwell_ms / 1000.0
        record.pop("dwell_ms", None)

    for src, dst in _MS_FIELDS:
        v = payload.get(src)
        if v is not None:
            record[dst] = v / 1000.0
            record.pop(src, None)

    is_tare, reason = looks_like_tare(record)
    if is_tare:
        log.info("dropped shot %s: %s", shot_id, reason)
        return

    # Reconcile final_weight_g against samples — shot/end's snapshot can
    # read low if the cup left the scale right before the publish.
    weights = [s.get("weight_g") for s in record["samples"] if s.get("weight_g") is not None]
    if weights:
        sample_max = max(weights)
        payload_final = record.get("final_weight_g") or 0
        if sample_max > payload_final + 0.5:
            record["final_weight_g"] = round(sample_max, 2)
            record["final_weight_source"] = "samples"
            log.info(
                "shot %s: reconciled final_weight_g %.2f → %.2fg (from samples)",
                shot_id, payload_final, sample_max,
            )

    inherited = latest_metadata()
    if inherited:
        record.setdefault("metadata", inherited)

    safe_id = re.sub(r"[^A-Za-z0-9_-]+", "_", shot_id)
    name = f"{utc_iso()}_{safe_id}.json"
    path = os.path.join(OUT, name)
    with open(path, "w") as f:
        json.dump(record, f, indent=2)
    log.info(
        "saved %s (%d samples, %sg in %ss)",
        name, len(record.get("samples", [])),
        record.get("final_weight_g"), record.get("duration_s"),
    )
    update_index(name)


def on_message(client, userdata, msg):
    if msg.topic == T_SCALE_W:
        handle_scale_weight(msg.payload)
        return
    if msg.topic == T_RAW:
        handle_raw_chunk(msg.payload)
        return

    try:
        payload = json.loads(msg.payload)
    except json.JSONDecodeError as e:
        log.warning("bad json on %s: %s", msg.topic, e)
        return

    if msg.topic == T_META_SET:
        file_name = payload.get("file")
        meta = payload.get("metadata") or {}
        if not file_name or not isinstance(meta, dict):
            log.warning("bad metadata payload: %r", payload)
            return
        ok, info = apply_metadata(file_name, meta)
        log.info("metadata for %s: %s", file_name, info)
        if ok:
            update_index(file_name)
        return

    if msg.topic == T_DELETE:
        file_name = payload.get("file")
        if not file_name:
            log.warning("bad delete payload: %r", payload)
            return
        ok, info = delete_shot(file_name)
        log.info("delete: %s", info)
        if ok:
            remove_from_index(file_name)
        return

    shot_id = str(payload.get("shot_id", "")).strip()
    if not shot_id:
        log.warning("missing shot_id on %s: %r", msg.topic, payload)
        return

    if msg.topic == T_START:
        handle_shot_start(shot_id, payload)
    elif msg.topic == T_SAMPLE:
        handle_shot_sample(shot_id, payload)
    elif msg.topic == T_END:
        handle_shot_end(shot_id, payload)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    os.makedirs(OUT, exist_ok=True)
    index_scan()
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
