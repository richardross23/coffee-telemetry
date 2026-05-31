"""MQTT subscriber: writes one JSON file per shot and maintains a history index.

Topics are accepted on either of the configured namespaces (see NAMESPACES),
e.g. coffee/* (round-display device) or coffee-pro/* (Lilygo Pro). Both can
publish simultaneously; we save the shot regardless of source. Acks are
returned on the same namespace the shot arrived from.

Live path:  <ns>/shot/{start,sample,end} → shots/<wallclock>_<id>.json
            (with a 10s rolling pre-shot weight buffer prepended).
Raw path:   <ns>/shot/raw chunks → reassemble → shots/<id>.json on the
            <ns>/shot/raw/complete marker, then ack on <ns>/shot/ack/<id>.
Edits:      <ns>/shot/metadata/set merges whitelisted fields into a saved
            shot; <ns>/shot/delete removes one.
"""

import datetime
import glob
import json
import logging
import math
import os
import re
import statistics
import sys
import time
from collections import defaultdict, deque

import paho.mqtt.client as mqtt

log = logging.getLogger("shot-logger")

OUT = os.environ.get("OUT_DIR", "/data/shots")
BROKER = os.environ.get("MQTT_BROKER", "mosquitto")
PORT = int(os.environ.get("MQTT_PORT", "1883"))

# Topic constants are the canonical (namespace-stripped) form used by the
# handler dispatch. Real subscriptions enumerate all NAMESPACES so the
# logger accepts publishes from either the round-display device or the
# Lilygo Pro running in parallel.
NAMESPACES = ("coffee", "coffee-pro")
T_START = "shot/start"
T_SAMPLE = "shot/sample"
T_END = "shot/end"
T_RAW = "shot/raw"
T_RAW_COMPLETE = "shot/raw/complete"
T_SCALE_W = "scale/weight"
T_META_SET = "shot/metadata/set"
T_DELETE = "shot/delete"


def _topic_suffix(topic: str) -> tuple[str | None, str | None]:
    """Return (namespace, suffix) for our topics, or (None, None) otherwise."""
    for ns in NAMESPACES:
        prefix = ns + "/"
        if topic.startswith(prefix):
            return ns, topic[len(prefix):]
    return None, None

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
PREPEND_MAX_WEIGHT_G = 5.0   # heavier samples in this window are cup-placements
MIN_FINAL_WEIGHT_G = 2.0     # below this, the "shot" is almost certainly a tare event
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
    """Detect tare events masquerading as shots.

    Uses max sample weight rather than shot/end's final_weight_g — that
    snapshot reads zero if the cup was lifted before publish.
    """
    samples = record.get("samples") or []
    if not samples:
        return True, "no samples"
    weights = [s.get("weight_g") for s in samples if s.get("weight_g") is not None]
    sample_max = max(weights) if weights else 0
    if sample_max < MIN_FINAL_WEIGHT_G:
        return True, f"max sample {sample_max:.2f}g < {MIN_FINAL_WEIGHT_G}g"
    return False, ""


def clip_outlier_samples(samples: list[dict]) -> list[dict]:
    """Drop samples outside WEIGHT_RANGE_G — Acaia reads a large negative
    when a cup is lifted off the scale post-tare, which is real (cup
    weight) but not extraction data."""
    lo, hi = WEIGHT_RANGE_G
    return [
        s for s in samples
        if s.get("weight_g") is None or lo <= s["weight_g"] <= hi
    ]


def trim_post_shot_anomalies(samples: list[dict], pump_off_s: float | None,
                              max_delta_g: float = 2.0) -> list[dict]:
    """Truncate samples after pump_off at the first weight jump >2g from
    the previous valid sample. Real after-drip is sub-gram drips and
    plateau republishes; anything bigger is the user touching the scale
    (grabbing the cup creates downward pressure that reads as +mass)."""
    if pump_off_s is None or not samples:
        return samples
    out = []
    prev_w = None
    for s in samples:
        t_s = (s.get("t_ms") or 0) / 1000.0
        w = s.get("weight_g")
        if t_s > pump_off_s and w is not None and prev_w is not None:
            if abs(w - prev_w) > max_delta_g:
                break
        out.append(s)
        if w is not None:
            prev_w = w
    return out


def channeling_count(samples: list[dict], first_drop_ms: int,
                       pump_off_s: float) -> int | None:
    """Count flow excursions ≥30% above the local 3s rolling mean during
    extraction (first drop +2s through pump_off). Two or more = likely
    channeling. Mirrors the dashboard's chart.channelingScore() algorithm."""
    if first_drop_ms is None or pump_off_s is None:
        return None
    fd_s = first_drop_ms / 1000.0
    # Re-derive flow with the same windowed-EMA + jitter handling
    # the dashboard uses, so the score matches what's rendered.
    pts = [(s["t_ms"] / 1000.0, s["weight_g"]) for s in samples
           if s.get("t_ms") is not None and s.get("weight_g") is not None]
    if len(pts) < 10:
        return None
    flow, smoothed, last_gap = [0.0] * len(pts), 0.0, 0
    for i, (t, w) in enumerate(pts):
        if i > 0:
            gap = t - pts[i - 1][0]
            if gap > 0.4 or gap < 0.05:
                last_gap = i
                flow[i] = smoothed
                continue
        t_start = t - 1.0
        j = i
        while j > last_gap and pts[j - 1][0] >= t_start:
            j -= 1
        dT, dW = t - pts[j][0], w - pts[j][1]
        raw = dW / dT if dT > 0 else 0.0
        if raw <= 8.0:
            smoothed = 0.4 * max(0.0, raw) + 0.6 * smoothed
        flow[i] = smoothed

    ext = [(pts[i][0], flow[i]) for i in range(len(pts))
           if fd_s + 2.0 <= pts[i][0] <= pump_off_s]
    if len(ext) < 8:
        return None
    mean_flow = sum(f for _, f in ext) / len(ext)
    if mean_flow <= 0.3:
        return None
    count = 0
    for t, f in ext:
        window = [fj for tj, fj in ext if abs(tj - t) <= 1.5]
        trend = sum(window) / len(window) if window else mean_flow
        if f - trend > 0.3 * mean_flow:
            count += 1
    return count


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

    # Acaia's auto-stop is the cleanest pour-end signal; fall back to last sample.
    acaia_stop_s = data.get("acaia_stop_at_s")
    if acaia_stop_s is not None and first_t is not None:
        pour_time_s = acaia_stop_s - first_t / 1000.0
    elif first_t is not None and last_t is not None:
        pour_time_s = (last_t - first_t) / 1000.0
    else:
        pour_time_s = None

    pump_off_s = data.get("pump_off_at_s")
    dwell_s = data.get("dwell_s")
    # Pure extraction window: first drop → pump off. Canonical
    # denominator for "average flow rate" — excludes dwell and
    # after-drip.
    extract_time_s = (
        pump_off_s - dwell_s
        if pump_off_s is not None and dwell_s is not None
        else None
    )
    channeling = channeling_count(samples, first_t, pump_off_s)

    saved_at = parse_ts(name)
    meta = data.get("metadata") or {}
    entry = {
        "file": name,
        "saved_at": saved_at,
        "shot_id": data.get("shot_id"),
        "duration_s": data.get("duration_s"),
        "pour_time_s": pour_time_s,
        "pump_off_at_s": pump_off_s,
        "last_drop_at_s": data.get("last_drop_at_s"),
        "pump_time_s": pump_off_s,
        "extract_time_s": extract_time_s,
        "channeling": channeling,
        "dwell_s": dwell_s,
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


# --- Puck Resistance Index ---------------------------------------------------
# A single z-scored composite of dwell + pour + (-)peak_flow_g_s. Two
# variants are computed and stored per shot — they answer different
# questions and should not be conflated.
#
#   resistance_index         baselined against ALL other same-bag shots
#                            ("is this puck on target vs how I usually
#                            pull this bag?") — prescriptive, drives the
#                            chip + grind-direction recommendation.
#                            Stable; doesn't drift with the last few shots.
#
#   resistance_index_rolling baselined against the last RI_WINDOW prior
#                            same-bag shots ("did this puck differ from
#                            the very recent ones?") — descriptive, drives
#                            the trend sparkline only. Reactive by design.
#
# Sign convention for both: RI > 0 = more resistive than baseline (puck
# tight / fine → consider coarser). RI < 0 = less resistive (puck loose
# / coarse → consider finer). Units: σ of the respective baseline.
#
# An earlier version used only the rolling RI for the chip — it reinforced
# drift instead of correcting toward the operator's normal. The absolute
# (per-bag) baseline is the one to act on; rolling is just for spotting
# session-scale drift.

RI_WINDOW = 7              # rolling-baseline window (sparkline)
RI_MIN_PRIORS_ROLLING = 5
RI_MIN_PRIORS_ABSOLUTE = 10  # demand a real population before z-scoring


def _bag_key(meta: dict) -> tuple:
    """Same bag = same brand, type, AND roast date. Different roast dates
    of the same coffee are different bags and reset both baselines."""
    if not meta:
        return ()
    return (meta.get("bean_brand") or "",
            meta.get("bean_type") or "",
            meta.get("roast_date") or "")


def compute_resistance_index(target: dict, baseline: list[dict],
                              min_priors: int) -> float | None:
    """RI = mean of z(dwell), z(pour), -z(peak_flow) against `baseline`.

    Returns None if target lacks any of the three inputs, or `baseline`
    has fewer than `min_priors` usable entries, or any input's SD across
    the baseline is zero (degenerate)."""
    td = target.get("dwell_s")
    tp = target.get("pour_time_s")
    tk = target.get("peak_flow_g_s")
    if td is None or tp is None or tk is None:
        return None

    def col(k):
        return [p[k] for p in baseline if p.get(k) is not None]
    dwells = col("dwell_s"); pours = col("pour_time_s"); peaks = col("peak_flow_g_s")
    if min(len(dwells), len(pours), len(peaks)) < min_priors:
        return None
    try:
        zd_sd = statistics.stdev(dwells)
        zp_sd = statistics.stdev(pours)
        zk_sd = statistics.stdev(peaks)
    except statistics.StatisticsError:
        return None
    if zd_sd == 0 or zp_sd == 0 or zk_sd == 0:
        return None
    zd = (td - statistics.mean(dwells)) / zd_sd
    zp = (tp - statistics.mean(pours))  / zp_sd
    zk = (tk - statistics.mean(peaks))  / zk_sd
    return (zd + zp - zk) / 3.0


def _annotate_resistance_indices(entries: list[dict]) -> None:
    """Compute both RI variants per entry. Mutates in place; call before
    sorting newest-first for write.

    Absolute baseline = all other same-bag entries (target excluded so
    extreme shots don't moderate their own z-score). Rolling baseline =
    last RI_WINDOW prior same-bag entries (chronological)."""
    by_bag: dict[tuple, list[dict]] = {}
    for e in entries:
        by_bag.setdefault(_bag_key(e.get("metadata") or {}), []).append(e)

    chrono_keys = {id(e): i for i, e in enumerate(
        sorted(entries, key=lambda x: x.get("saved_at") or x.get("file") or ""))}

    for bag_key, bag_entries in by_bag.items():
        # Sort the bag chronologically so "prior" is unambiguous for rolling.
        bag_sorted = sorted(bag_entries, key=lambda x: x.get("saved_at") or x.get("file") or "")
        for i, e in enumerate(bag_sorted):
            # Absolute: all OTHER entries in the bag.
            others = bag_sorted[:i] + bag_sorted[i + 1:]
            ri_abs = compute_resistance_index(e, others, RI_MIN_PRIORS_ABSOLUTE)
            # Rolling: the last RI_WINDOW priors before this entry.
            window = bag_sorted[max(0, i - RI_WINDOW):i]
            ri_roll = compute_resistance_index(e, window, RI_MIN_PRIORS_ROLLING)

            if ri_abs is not None:
                e["resistance_index"] = round(ri_abs, 3)
            else:
                e.pop("resistance_index", None)
            if ri_roll is not None:
                e["resistance_index_rolling"] = round(ri_roll, 3)
            else:
                e.pop("resistance_index_rolling", None)


# --- Index cache: in-memory mirror of shots/index.json -----------------------
_index: dict[str, dict] = {}


def _read_shot(path: str) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.warning("skipping %s: %s", os.path.basename(path), e)
        return None


def index_scan() -> None:
    """Populate _index from disk. Bare <shot_id>.json files (raw archives)
    aren't shown in history — only timestamped summary files are."""
    _index.clear()
    for path in glob.glob(os.path.join(OUT, "*.json")):
        name = os.path.basename(path)
        if name == INDEX or not FILENAME_TS_RE.match(name):
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
    """Atomically write the index sorted by filename desc (newest first).

    Resistance Index is computed here (not in build_index_entry) because it
    depends on prior same-bag shots — needs the full collection in hand."""
    entries = list(_index.values())
    _annotate_resistance_indices(entries)
    entries.sort(key=lambda e: e.get("file") or "", reverse=True)
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
    qos = [(T_START, 0), (T_SAMPLE, 0), (T_END, 0),
           (T_RAW, 0), (T_RAW_COMPLETE, 1),  # QoS 1 — ack handshake
           (T_SCALE_W, 0), (T_META_SET, 0), (T_DELETE, 0)]
    client.subscribe([(f"{ns}/{suf}", q) for ns in NAMESPACES for suf, q in qos])


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


# Reassemble raw shot chunks; persist on the complete marker; ack after disk.
RAW_TTL_SEC = 120.0
_raw_chunks: dict[str, dict] = {}    # shot_id -> {chunks: {idx: bytes}, ts: float}


def handle_raw_chunk(payload: bytes) -> None:
    parts = payload.split(b"|", 3)
    if len(parts) != 4:
        log.warning("raw: malformed chunk %r", payload[:80])
        return
    try:
        shot_id = parts[0].decode("ascii")
        idx = int(parts[1])
    except (ValueError, UnicodeDecodeError):
        log.warning("raw: bad header %r", payload[:80])
        return

    bucket = _raw_chunks.setdefault(shot_id, {"chunks": {}, "ts": time.time()})
    bucket["chunks"][idx] = parts[3]
    bucket["ts"] = time.time()  # refresh on every chunk so long shots don't get evicted mid-stream
    _evict_stale_raw()


def handle_raw_complete(client, namespace: str, payload: bytes) -> None:
    try:
        marker = json.loads(payload)
    except json.JSONDecodeError as e:
        log.warning("raw/complete: bad JSON: %s", e)
        return
    shot_id = marker.get("shot_id")
    expected_chunks = marker.get("chunks")
    expected_bytes = marker.get("bytes")
    if shot_id is None or expected_chunks is None or expected_bytes is None:
        log.warning("raw/complete: missing fields in %r", marker)
        return
    sid = str(shot_id)
    bucket = _raw_chunks.pop(sid, None)
    have = len(bucket["chunks"]) if bucket else 0
    if have != expected_chunks:
        log.warning("raw %s: have %d/%d chunks, not acking (device will retry)",
                    sid, have, expected_chunks)
        return
    body = b"".join(bucket["chunks"][i] for i in range(expected_chunks))
    if len(body) != expected_bytes:
        log.warning("raw %s: byte mismatch (got %d, expected %d), not acking",
                    sid, len(body), expected_bytes)
        return
    try:
        raw = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        log.warning("raw %s: parse failed (%s), not acking", sid, e)
        return
    # Disk write must succeed BEFORE we ack — the device deletes its
    # local copy on ack receipt, so an ack-then-fail leaves no trace.
    path = os.path.join(OUT, f"{sid}.json")
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(raw, f, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        log.error("raw %s: write failed (%s), not acking", sid, e)
        return
    client.publish(f"{namespace}/shot/ack/{sid}", b"", qos=1)
    log.info("raw %s: archived (%d bytes), acked on %s/", sid, len(body), namespace)


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

    buf = shots_in_flight.pop(shot_id, None)
    record = dict(payload)
    if buf is not None:
        record.setdefault("samples", buf["prepend"] + buf["samples"])
        if buf["start"] and "tank_pct_at_start" not in record:
            record["tank_pct_at_start"] = buf["start"].get("tank_pct_at_start")
    else:
        record.setdefault("samples", [])

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

    # Drop cup-lift / out-of-range samples without rejecting the whole
    # shot — Acaia reads a large negative when the cup leaves the
    # scale post-tare, but the extraction data before that is real.
    n_before = len(record["samples"])
    record["samples"] = clip_outlier_samples(record["samples"])
    n_clipped = n_before - len(record["samples"])
    if n_clipped:
        log.info("shot %s: clipped %d out-of-range sample(s)", shot_id, n_clipped)

    # Trim any cup-handling after the shot proper — sudden weight jumps
    # after pump_off are the user touching the scale.
    n_before = len(record["samples"])
    record["samples"] = trim_post_shot_anomalies(
        record["samples"], record.get("pump_off_at_s"),
    )
    n_trimmed = n_before - len(record["samples"])
    if n_trimmed:
        log.info("shot %s: trimmed %d post-shot anomalous sample(s)", shot_id, n_trimmed)

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
    ns, suffix = _topic_suffix(msg.topic)
    if suffix is None:
        return
    if suffix == T_SCALE_W:
        handle_scale_weight(msg.payload)
        return
    if suffix == T_RAW:
        handle_raw_chunk(msg.payload)
        return
    if suffix == T_RAW_COMPLETE:
        handle_raw_complete(client, ns, msg.payload)
        return

    try:
        payload = json.loads(msg.payload)
    except json.JSONDecodeError as e:
        log.warning("bad json on %s: %s", msg.topic, e)
        return

    if suffix == T_META_SET:
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

    if suffix == T_DELETE:
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

    if suffix == T_START:
        handle_shot_start(shot_id, payload)
    elif suffix == T_SAMPLE:
        handle_shot_sample(shot_id, payload)
    elif suffix == T_END:
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
