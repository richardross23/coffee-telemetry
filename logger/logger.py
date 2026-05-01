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
# Within the prepend lookback window, real first-drop weight should never
# exceed a few grams. If we see anything heavier it's the user placing the
# cup on the scale (40-200g) before taring — drop those samples so the
# tare spike doesn't pollute the saved curve or the first-drop detection.
PREPEND_MAX_WEIGHT_G = 5.0

# Tare-event filter — the firmware fires shot/start on weight delta,
# so taring the scale (or fiddling with the cup) currently produces
# bogus "shots" that return to zero with wild excursions in the curve.
# Drop them at save time rather than poison the history view.
MIN_FINAL_WEIGHT_G = 2.0
WEIGHT_RANGE_G = (-5.0, 200.0)


def looks_like_tare(record: dict) -> tuple[bool, str]:
    """Return (is_tare, reason) for a shot record about to be saved.

    The firmware's shot/end final_weight_g is a snapshot of the scale
    at exactly that moment — if the user lifts the cup, or BLE state is
    stale at end-time, it can read 0 even though real flow happened.
    Use max(samples.weight_g) as the authoritative final weight: it's
    sourced from the same MQTT stream the dashboard renders live, so if
    the curve was real on screen, the data's real here too.
    """
    samples = record.get("samples") or []
    if not samples:
        return True, "no samples"
    sample_weights = [s.get("weight_g") for s in samples if s.get("weight_g") is not None]
    sample_max = max(sample_weights) if sample_weights else 0
    if sample_max < MIN_FINAL_WEIGHT_G:
        return True, f"max sample weight {sample_max:.2f}g (below {MIN_FINAL_WEIGHT_G}g)"
    lo, hi = WEIGHT_RANGE_G
    for w in sample_weights:
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


def last_drop_t_ms(
    samples: list[dict],
    pump_off_t_ms: int | None,
    threshold_g: float = 0.1,
    settle_window_ms: int = 1000,
) -> int | None:
    """Find first t_ms after pump_off where weight stops changing.

    Stabilisation = max(weight) − min(weight) ≤ threshold_g over a sliding
    window that spans ≥ 80 % of settle_window_ms. Returns the anchor t_ms
    of the first qualifying window, or None if the user lifted the cup
    before the tail settled / sample stream cut off mid-tail.
    """
    if pump_off_t_ms is None or not samples:
        return None
    post = [
        ((s.get("t_ms") or 0), s.get("weight_g"))
        for s in samples
        if (s.get("t_ms") or 0) >= pump_off_t_ms and s.get("weight_g") is not None
    ]
    if len(post) < 2:
        return None
    min_span_ms = settle_window_ms * 0.8
    n = len(post)
    for i in range(n):
        anchor_t, _ = post[i]
        weights = []
        last_t = anchor_t
        for j in range(i, n):
            t, w = post[j]
            if t - anchor_t > settle_window_ms:
                break
            weights.append(w)
            last_t = t
        # Need both: enough time covered AND weights stable within threshold
        if (last_t - anchor_t) >= min_span_ms and len(weights) >= 2:
            if (max(weights) - min(weights)) <= threshold_g:
                return anchor_t
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
        # Pour-time end-point: prefer Acaia's auto-stop event over the
        # last-sample fallback. Acaia's algorithm is more authoritative
        # than our 'first_drop → last sample' heuristic — it knows the
        # difference between dribbles and a finished shot.
        acaia_stop_s = data.get("acaia_stop_at_s")
        if acaia_stop_s is not None and first_t is not None:
            pour_time_s = acaia_stop_s - (first_t / 1000.0)
        elif first_t is not None and last_t is not None:
            pour_time_s = (last_t - first_t) / 1000.0
        else:
            pour_time_s = None
        meta = data.get("metadata") or {}
        saved_at = parse_ts(name)
        # Derive the timing trifecta when we have all the events.
        # pump_time = pump_on → pump_off (the canonical barista shot duration).
        # Under the pump-driven firmware, shot_start fires at pump_on, so
        # pump_off_at_s is already (pump_off − pump_on) = pump_time.
        # Older shots have been backfilled to the same frame, so no
        # dwell add needed any more.
        dwell_s = data.get("dwell_s")
        pump_off_s = data.get("pump_off_at_s")
        last_drop_s = data.get("last_drop_at_s")
        pump_time_s = pump_off_s if pump_off_s is not None else None
        # Clamp after_drip to >= 0: the rare lungo case where the puck
        # stops giving before the lever comes down would produce a small
        # negative; treat it as 'no after-drip' for display purposes
        # (per firmware's edge-case note).
        after_drip_s = (
            max(0.0, last_drop_s - pump_off_s)
            if pump_off_s is not None and last_drop_s is not None
            else None
        )
        # Flag suspect pump_off detection: if the LIS3DH disengage fired
        # more than 2 s after the last weight sample, the firmware likely
        # held the "pump on" state too long (post-shot accelerometer
        # noise above the disengage threshold). Real pump-off is closer
        # to the last weight movement.
        pump_off_suspect = (
            pump_off_s is not None
            and last_t is not None
            and (pump_off_s - last_t / 1000.0) > 2.0
        )
        entry = {
            "file": name,
            "saved_at": saved_at,
            "shot_id": data.get("shot_id"),
            "duration_s": data.get("duration_s"),
            "pour_time_s": pour_time_s,
            "pump_off_at_s": pump_off_s,
            "last_drop_at_s": last_drop_s,
            "pump_time_s": pump_time_s,
            "pump_off_suspect": pump_off_suspect,
            "after_drip_s": after_drip_s,
            "dwell_s": dwell_s,
            "dwell_suspect": data.get("dwell_suspect", False),
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
        # Skip any sample where |weight| exceeds the espresso first-drop
        # range — that's almost certainly cup placement or tare events
        # captured incidentally by the always-on weight stream.
        # Note: as of the pump-driven firmware refactor, shot/start now
        # fires at pump_on (not first weight rise), so dwell_ms is no
        # longer in this payload — it lands in shot/end instead.
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
        print(f"shot {shot_id} started ({len(prepend)} prepend samples)", flush=True)

    elif msg.topic == T_SAMPLE:
        shots_in_flight[shot_id]["samples"].append({
            "t_ms": payload.get("t_ms"),
            "weight_g": payload.get("weight_g"),
            "flow_g_s": payload.get("flow_g_s"),
        })

    elif msg.topic == T_END:
        # Aborted shots (firmware decided this wasn't a real shot —
        # backflush, blind basket, accidental lever-bump, descale, or
        # explicit acaia_tare). Drop without running the heuristic
        # tare filter; the firmware's verdict is authoritative.
        if payload.get("aborted"):
            shots_in_flight.pop(shot_id, None)
            reason = payload.get("reason", "?")
            dur = payload.get("duration_s")
            wt = payload.get("final_weight_g")
            print(
                f"dropped shot {shot_id}: aborted by firmware "
                f"(reason={reason}, duration={dur}s, weight={wt}g)",
                flush=True,
            )
            return

        buf = shots_in_flight.pop(
            shot_id, {"start": None, "samples": [], "prepend": []}
        )
        record = dict(payload)
        record.setdefault("samples", buf["prepend"] + buf["samples"])
        if buf["start"] and "tank_pct_at_start" not in record:
            record["tank_pct_at_start"] = buf["start"].get("tank_pct_at_start")

        # dwell_ms now lands in shot/end (was in shot/start under the old
        # firmware — the pump-driven refactor moved it because dwell can't
        # be known at shot-start any more, only at first-drop arrival).
        dwell_ms = payload.get("dwell_ms")
        if dwell_ms:
            record["dwell_s"] = dwell_ms / 1000.0
            if dwell_ms > 15000:
                record["dwell_suspect"] = True
            # Don't keep the duplicate ms field in the saved record
            record.pop("dwell_ms", None)

        # Acaia button events relayed by firmware. Convert ms → seconds so
        # they sit alongside pour_time_s / dwell_s naturally. Fields are
        # absent if the corresponding BLE event didn't fire during the
        # shot window — leave them out of the saved record entirely.
        # NOTE: Acaia auto-stop turned out not to broadcast over BLE; these
        # fields will only populate on manual button presses. Kept in the
        # schema as opportunistic signal.
        for src, dst in (
            ("acaia_start_at_ms", "acaia_start_at_s"),
            ("acaia_stop_at_ms", "acaia_stop_at_s"),
            ("pump_off_at_ms", "pump_off_at_s"),
            # Firmware now ships last_drop_at_ms directly: timestamp of
            # the last weight-increasing sample (Δ ≥ 0.1g) seen during
            # EXTRACT or AFTER_DRIP. Authoritative — replaces the
            # sample-scanning fallback below, which is dead under the
            # pump-driven refactor since samples cut off at pump_off.
            ("last_drop_at_ms", "last_drop_at_s"),
        ):
            v = payload.get(src)
            if v is not None:
                record[dst] = v / 1000.0
                record.pop(src, None)

        # Belt-and-braces: if firmware didn't provide last_drop_at_ms
        # (older firmware, or a future regression), fall back to scanning
        # samples post pump_off. Won't fire with current firmware since
        # the field is always present on real shots.
        if "last_drop_at_s" not in record:
            pump_off_ms = (
                int(record["pump_off_at_s"] * 1000)
                if "pump_off_at_s" in record
                else None
            )
            last_drop = last_drop_t_ms(record["samples"], pump_off_ms)
            if last_drop is not None:
                record["last_drop_at_s"] = last_drop / 1000.0

        is_tare, reason = looks_like_tare(record)
        if is_tare:
            print(f"dropped shot {shot_id}: {reason}", flush=True)
            return

        # Reconcile final_weight_g against samples — same defensive logic
        # as the tare filter. If shot/end's snapshot is suspicious (zero
        # or noticeably under the max sample), prefer the sample-derived
        # value. Common when the user lifts the cup right before shot/end.
        sample_weights = [s.get("weight_g") for s in record["samples"] if s.get("weight_g") is not None]
        if sample_weights:
            sample_max = max(sample_weights)
            payload_final = record.get("final_weight_g") or 0
            if sample_max > payload_final + 0.5:
                record["final_weight_g"] = round(sample_max, 2)
                record["final_weight_source"] = "samples"
                print(
                    f"  reconciled final_weight_g: shot/end said {payload_final:.2f}g, "
                    f"samples max {sample_max:.2f}g — using samples",
                    flush=True,
                )

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
