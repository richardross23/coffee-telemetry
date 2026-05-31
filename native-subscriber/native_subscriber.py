"""ESPHome Native API subscriber.

Connects to the device on port 6053 and produces the same per-shot JSON
files the MQTT logger produces, so the dashboard's history / replay /
chart / channeling / Decent export all work unchanged.

Shot lifecycle is driven by text_sensor.shot_state transitions:

    IDLE → PREINFUSION → EXTRACTION → AFTER_DRIP → DONE → IDLE

On IDLE → PREINFUSION we start a per-shot buffer, prepend up to 10 s of
pre-shot scale samples, and stream sensor.scale_weight updates into it
until DONE. text_sensor.shot_summary lands once with the firmware-side
summary (final weight, dwell, pump-off, etc); on DONE we combine the two
and write the file.

Raw archive (full accel + weight + events trace) lives on the device
under /shots/<id>.json and is fetched over HTTP once firmware Stage 14
adds the GET / DELETE handlers. Until then, we just have the live
summary path.
"""

import asyncio
import datetime
import glob
import json
import logging
import math
import os
import re
import sys
import time
from collections import deque

import aiohttp
from aioesphomeapi import APIClient, ReconnectLogic

log = logging.getLogger("native-subscriber")

DEVICE_HOST = os.environ.get("DEVICE_HOST", "192.168.1.27")
DEVICE_PORT = int(os.environ.get("DEVICE_PORT", "6053"))
DEVICE_HTTP_PORT = int(os.environ.get("DEVICE_HTTP_PORT", "8080"))
DEVICE_PASSWORD = os.environ.get("DEVICE_PASSWORD", "")
# Noise PSK from the device's `api.encryption.key`. Required if the
# firmware has encryption enabled (it always is on the Pro by default).
DEVICE_NOISE_PSK = os.environ.get("DEVICE_NOISE_PSK", "") or None
DEVICE_NAME = os.environ.get("DEVICE_NAME", "coffee-pro")
OUT_DIR = os.environ.get("OUT_DIR", "/data/shots")

SCALE_BUFFER_SEC = 30.0
PREPEND_LOOKBACK_SEC = 10.0
PREPEND_MAX_WEIGHT_G = 5.0
WEIGHT_RANGE_G = (-5.0, 200.0)

# Entity object_ids we care about. Keys filled at connect time.
WATCH = {
    "scale_weight": None,
    "scale_flow": None,
    "tank_percent_full": None,
    "shot_state": None,
    "shot_summary": None,
    "shot_archive_status": None,
}


# --- in-memory state ---------------------------------------------------------

_scale_buffer: deque = deque()   # (epoch_ts, weight_g) rolling 30 s
_last_flow: float = 0.0
_tank_pct: float | None = None
_prev_shot_state: str = "IDLE"


class ShotInFlight:
    __slots__ = ("start_wall", "samples", "summary")

    def __init__(self, start_wall: float, prepend: list[dict]):
        self.start_wall = start_wall
        self.samples: list[dict] = list(prepend)
        self.summary: dict | None = None


_shot: ShotInFlight | None = None


# --- sample / state handlers -------------------------------------------------

def _push_scale_buffer(now: float, w: float) -> None:
    _scale_buffer.append((now, w))
    cutoff = now - SCALE_BUFFER_SEC
    while _scale_buffer and _scale_buffer[0][0] < cutoff:
        _scale_buffer.popleft()


def _on_scale_weight(value) -> None:
    if value is None: return
    try: w = float(value)
    except (TypeError, ValueError): return
    if math.isnan(w): return
    now = time.time()
    _push_scale_buffer(now, w)
    if _shot is not None:
        t_ms = int((now - _shot.start_wall) * 1000)
        _shot.samples.append({"t_ms": t_ms, "weight_g": w, "flow_g_s": _last_flow})


def _on_scale_flow(value) -> None:
    global _last_flow
    if value is None: return
    try: f = float(value)
    except (TypeError, ValueError): return
    if not math.isnan(f):
        _last_flow = f


def _on_tank_pct(value) -> None:
    global _tank_pct
    if value is None: return
    try: _tank_pct = float(value)
    except (TypeError, ValueError): pass


def _on_shot_state(value) -> None:
    global _shot, _prev_shot_state
    if not isinstance(value, str): return
    new_state = value.strip()
    if new_state == _prev_shot_state: return

    log.info("shot_state: %s → %s", _prev_shot_state, new_state)

    # Open a new shot buffer when we see the first sign of a shot in
    # progress — usually IDLE → PREINFUSION, but also IDLE → EXTRACTION
    # if we reconnected mid-shot and missed the PREINFUSION transition.
    if _shot is None and new_state in ("PREINFUSION", "EXTRACTION"):
        start = time.time()
        cutoff = start - PREPEND_LOOKBACK_SEC
        prepend = [
            {"t_ms": int((ts - start) * 1000), "weight_g": w, "flow_g_s": 0.0}
            for ts, w in _scale_buffer
            if ts >= cutoff and abs(w) <= PREPEND_MAX_WEIGHT_G
        ]
        _shot = ShotInFlight(start_wall=start, prepend=prepend)
        log.info("shot started at %s (%d prepend samples)", new_state, len(prepend))

    elif new_state == "DONE":
        _maybe_finalize()

    elif new_state == "IDLE" and _shot is not None:
        # Belt-and-braces: clear if state machine returns to IDLE without DONE.
        log.warning("returned to IDLE with in-flight shot; discarding")
        _shot = None

    _prev_shot_state = new_state


_PENDING_RE = re.compile(r"pending\s+id\s*=\s*(\d+)", re.IGNORECASE)
_raw_lock = asyncio.Lock()


def _on_shot_archive_status(value) -> None:
    """text_sensor.shot_archive_status — 'idle' or 'pending id=<n>'.
    When pending, fetch the raw JSON, write to disk, then DELETE."""
    if not isinstance(value, str):
        return
    m = _PENDING_RE.search(value)
    if not m:
        return  # 'idle' or unrecognised
    shot_id = int(m.group(1))
    # Fire-and-forget — the state callback can't await; spin a task.
    asyncio.create_task(_fetch_and_archive(shot_id))


async def _fetch_and_archive(shot_id: int) -> None:
    """GET the raw JSON, save to OUT_DIR/<shot_id>.json, then DELETE.
    Lock prevents overlapping fetches if the status sensor flaps."""
    async with _raw_lock:
        path = os.path.join(OUT_DIR, f"{shot_id}.json")
        if os.path.exists(path):
            log.info("raw %s: already archived locally; deleting on device", shot_id)
            await _delete_remote(shot_id)
            return
        url = f"http://{DEVICE_HOST}:{DEVICE_HTTP_PORT}/shots/{shot_id}.json"
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.get(url) as r:
                    if r.status != 200:
                        log.warning("raw %s: GET %s → %d", shot_id, url, r.status)
                        return
                    body = await r.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("raw %s: fetch failed: %s", shot_id, e)
            return
        # Disk write must succeed BEFORE we DELETE; device retains otherwise.
        tmp = path + ".tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(body)
            os.replace(tmp, path)
        except OSError as e:
            log.error("raw %s: write failed: %s", shot_id, e)
            return
        log.info("raw %s: archived (%d bytes)", shot_id, len(body))
        await _delete_remote(shot_id)


async def _delete_remote(shot_id: int) -> None:
    url = f"http://{DEVICE_HOST}:{DEVICE_HTTP_PORT}/shots/{shot_id}.json"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.delete(url) as r:
                if r.status not in (200, 204):
                    log.warning("raw %s: DELETE → %d (will retry on next status emit)",
                                shot_id, r.status)
                else:
                    log.info("raw %s: acked on device", shot_id)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("raw %s: DELETE failed: %s", shot_id, e)


def _on_shot_summary(value) -> None:
    global _shot
    if not isinstance(value, str) or not value.strip(): return
    try:
        summary = json.loads(value)
    except json.JSONDecodeError as e:
        log.warning("shot_summary: bad JSON: %s", e)
        return
    # Summary can land before we ever saw a state transition (subscriber
    # restarted mid-shot, or missed PREINFUSION while disconnected). In
    # that case build a minimal shot from the summary alone — no live
    # sample stream, but the metadata still lands in history.
    if _shot is None:
        log.info("shot_summary arrived without a tracked shot; saving from summary alone")
        _shot = ShotInFlight(start_wall=time.time(), prepend=[])
    _shot.summary = summary
    log.info("shot_summary: shot_id=%s aborted=%s final=%sg",
             summary.get("shot_id"), summary.get("aborted"),
             summary.get("final_weight_g"))
    # State might have already transitioned to DONE before this arrived.
    if _prev_shot_state == "DONE":
        _maybe_finalize()


# --- sample cleaning + persistence -------------------------------------------

def _clip_outliers(samples: list[dict]) -> list[dict]:
    """Drop samples outside WEIGHT_RANGE_G — cup-lift events post-tare
    read as a large negative."""
    lo, hi = WEIGHT_RANGE_G
    return [s for s in samples
            if s.get("weight_g") is None or lo <= s["weight_g"] <= hi]


INDEX_NAME = "index.json"
FILENAME_TS_RE = re.compile(r"^(\d{8}T\d{6}Z)_")


def _parse_ts(filename: str) -> str:
    m = FILENAME_TS_RE.match(filename)
    if not m: return ""
    s = m.group(1)
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}T{s[9:11]}:{s[11:13]}:{s[13:15]}Z"


def _first_drop_t_ms(samples: list[dict], threshold: float = 0.1) -> int | None:
    consecutive = 0
    for i, s in enumerate(samples):
        if (s.get("weight_g") or 0) >= threshold:
            consecutive += 1
            if consecutive >= 2: return samples[i - 1].get("t_ms")
        else: consecutive = 0
    return None


def _index_entry(name: str, data: dict) -> dict:
    """Minimal index entry — mirrors the MQTT shot-logger's build_index_entry
    so the dashboard's history list reads either source identically."""
    samples = data.get("samples") or []
    first_t = _first_drop_t_ms(samples)
    last_t = samples[-1].get("t_ms") if samples else None
    acaia_stop_s = data.get("acaia_stop_at_s")
    if acaia_stop_s is not None and first_t is not None:
        pour_time_s = acaia_stop_s - first_t / 1000.0
    elif first_t is not None and last_t is not None:
        pour_time_s = (last_t - first_t) / 1000.0
    else:
        pour_time_s = None
    pump_off_s = data.get("pump_off_at_s")
    dwell_s = data.get("dwell_s")
    extract_time_s = (pump_off_s - dwell_s
                      if pump_off_s is not None and dwell_s is not None else None)
    saved_at = _parse_ts(name)
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
    # Bean label / brew_ratio_str / days_off_roast — derived from metadata.
    dose = meta.get("ground_weight_g") or meta.get("bean_weight_g")
    final_w = data.get("final_weight_g")
    if dose and final_w and dose > 0:
        entry["brew_ratio_str"] = f"1:{(final_w / dose):.2f}"
    roast = meta.get("roast_date")
    if roast and saved_at:
        try:
            roast_d = datetime.date.fromisoformat(roast)
            saved_d = datetime.datetime.fromisoformat(
                saved_at.replace("Z", "+00:00")).date()
            entry["days_off_roast"] = (saved_d - roast_d).days
        except (ValueError, TypeError): pass
    label = " · ".join(p for p in (meta.get("bean_brand"), meta.get("bean_type")) if p)
    if label: entry["bean_label"] = label
    return entry


# --- Puck Resistance Index (mirrors logger.py:compute_resistance_index) ----
# Composite z-score of dwell + pour − peak_flow_g_s. Two variants:
#   resistance_index         vs ALL other same-bag entries (absolute) —
#                            drives the chip + grind-direction recommendation.
#   resistance_index_rolling vs the last RI_WINDOW prior same-bag entries —
#                            drives the trend sparkline only.
# See logger.py for the rationale. Kept in sync — different Docker images.

import statistics  # noqa: E402

RI_WINDOW = 7
RI_MIN_PRIORS_ROLLING = 5
RI_MIN_PRIORS_ABSOLUTE = 10


def _bag_key(meta: dict) -> tuple:
    if not meta:
        return ()
    return (meta.get("bean_brand") or "",
            meta.get("bean_type") or "",
            meta.get("roast_date") or "")


def _compute_resistance_index(target: dict, baseline: list[dict], min_priors: int):
    td = target.get("dwell_s")
    tp = target.get("pour_time_s")
    tk = target.get("peak_flow_g_s")
    if td is None or tp is None or tk is None:
        return None
    def col(k):
        return [p[k] for p in baseline if p.get(k) is not None]
    d = col("dwell_s"); p = col("pour_time_s"); k = col("peak_flow_g_s")
    if min(len(d), len(p), len(k)) < min_priors:
        return None
    try:
        d_sd, p_sd, k_sd = statistics.stdev(d), statistics.stdev(p), statistics.stdev(k)
    except statistics.StatisticsError:
        return None
    if d_sd == 0 or p_sd == 0 or k_sd == 0:
        return None
    zd = (td - statistics.mean(d)) / d_sd
    zp = (tp - statistics.mean(p)) / p_sd
    zk = (tk - statistics.mean(k)) / k_sd
    return (zd + zp - zk) / 3.0


def _annotate_resistance_indices(entries: list[dict]) -> None:
    by_bag: dict[tuple, list[dict]] = {}
    for e in entries:
        by_bag.setdefault(_bag_key(e.get("metadata") or {}), []).append(e)
    for bag_entries in by_bag.values():
        bag_sorted = sorted(bag_entries,
                            key=lambda x: x.get("saved_at") or x.get("file") or "")
        for i, e in enumerate(bag_sorted):
            others = bag_sorted[:i] + bag_sorted[i + 1:]
            ri_abs = _compute_resistance_index(e, others, RI_MIN_PRIORS_ABSOLUTE)
            window = bag_sorted[max(0, i - RI_WINDOW):i]
            ri_roll = _compute_resistance_index(e, window, RI_MIN_PRIORS_ROLLING)
            if ri_abs is not None: e["resistance_index"] = round(ri_abs, 3)
            else: e.pop("resistance_index", None)
            if ri_roll is not None: e["resistance_index_rolling"] = round(ri_roll, 3)
            else: e.pop("resistance_index_rolling", None)


def _rebuild_index() -> None:
    """Full disk scan, write shots/index.json. Cheap at our shot volume."""
    entries = []
    for path in glob.glob(os.path.join(OUT_DIR, "*.json")):
        name = os.path.basename(path)
        if name == INDEX_NAME or not FILENAME_TS_RE.match(name):
            continue
        try:
            with open(path) as f: data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        entries.append(_index_entry(name, data))
    _annotate_resistance_indices(entries)
    entries.sort(key=lambda e: e["file"], reverse=True)
    tmp = os.path.join(OUT_DIR, INDEX_NAME + ".tmp")
    with open(tmp, "w") as f: json.dump(entries, f, indent=2)
    os.replace(tmp, os.path.join(OUT_DIR, INDEX_NAME))


def _trim_post_shot_anomalies(samples: list[dict], pump_off_s: float | None,
                                max_delta_g: float = 3.0) -> list[dict]:
    """Truncate samples after pump_off at the first weight jump >3g —
    that's the user touching the scale (cup re-placement, etc)."""
    if pump_off_s is None or not samples:
        return samples
    out, prev = [], None
    for s in samples:
        t_s = (s.get("t_ms") or 0) / 1000.0
        w = s.get("weight_g")
        if t_s > pump_off_s and w is not None and prev is not None:
            if abs(w - prev) > max_delta_g:
                break
        out.append(s)
        if w is not None:
            prev = w
    return out


def _maybe_finalize() -> None:
    """Write the shot file when both DONE state and shot_summary are in."""
    global _shot
    if _shot is None or _shot.summary is None:
        return

    s = _shot.summary
    record: dict = {
        "shot_id": s.get("shot_id"),
        "duration_s": s.get("duration_s"),
        "final_weight_g": s.get("final_weight_g"),
        "peak_flow_g_s": s.get("peak_flow_g_s"),
        "samples": _shot.samples,
    }
    tank = s.get("tank_pct_at_start")
    if tank is None or tank < 0:
        tank = _tank_pct
    if tank is not None:
        record["tank_pct_at_start"] = tank

    # ms → s for the canonical fields the dashboard reads.
    if (d := s.get("dwell_ms", 0)) > 0:
        record["dwell_s"] = d / 1000.0
    if (d := s.get("pump_off_at_ms", 0)) > 0:
        record["pump_off_at_s"] = d / 1000.0
    if (d := s.get("last_drop_at_ms", -1)) >= 0:
        record["last_drop_at_s"] = d / 1000.0
    if s.get("aborted"):
        record["aborted"] = True
        record["reason"] = s.get("reason", "")

    # Clean up cup-lift / accidental-touch samples; matches MQTT logger behaviour.
    record["samples"] = _clip_outliers(record["samples"])
    record["samples"] = _trim_post_shot_anomalies(
        record["samples"], record.get("pump_off_at_s"),
    )

    # Drop aborted shots from the saved set — same convention as the MQTT
    # logger. The raw archive on the device retains them for calibration.
    if record.get("aborted"):
        log.info("dropped shot %s: aborted (reason=%s)",
                 record["shot_id"], record.get("reason", "?"))
        _shot = None
        return

    # Filename anchored on shot_start_utc when available, fall back to now.
    utc = s.get("shot_start_utc", 0)
    if utc and utc > 0:
        ts = datetime.datetime.fromtimestamp(utc, datetime.timezone.utc)
    else:
        ts = datetime.datetime.now(datetime.timezone.utc)
    name = ts.strftime("%Y%m%dT%H%M%SZ") + f"_{s.get('shot_id')}.json"
    path = os.path.join(OUT_DIR, name)

    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(record, f, indent=2)
    os.replace(tmp, path)
    log.info("saved %s (%d samples, %sg in %ss)",
             name, len(record["samples"]),
             record.get("final_weight_g"), record.get("duration_s"))
    _rebuild_index()
    _shot = None


# --- aioesphomeapi plumbing --------------------------------------------------

def _dispatch_state(state) -> None:
    key = state.key
    if key == WATCH["scale_weight"]:
        _on_scale_weight(state.state)
    elif key == WATCH["scale_flow"]:
        _on_scale_flow(state.state)
    elif key == WATCH["tank_percent_full"]:
        _on_tank_pct(state.state)
    elif key == WATCH["shot_state"]:
        _on_shot_state(state.state)
    elif key == WATCH["shot_summary"]:
        _on_shot_summary(state.state)
    elif key == WATCH["shot_archive_status"]:
        _on_shot_archive_status(state.state)


async def _run() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    client = APIClient(DEVICE_HOST, DEVICE_PORT, DEVICE_PASSWORD,
                       client_info="coffee-telemetry-subscriber",
                       noise_psk=DEVICE_NOISE_PSK)

    async def on_connect() -> None:
        log.info("connected to %s:%s", DEVICE_HOST, DEVICE_PORT)
        entities, _ = await client.list_entities_services()
        log.info("entity surface (%d):", len(entities))
        for e in entities:
            log.info("  %s.%s (key=%d)",
                     type(e).__name__.replace('Info', '').lower(),
                     e.object_id, e.key)
        # Reset all watch keys then assign by object_id.
        for k in WATCH: WATCH[k] = None
        for e in entities:
            if e.object_id in WATCH:
                WATCH[e.object_id] = e.key
        missing = [k for k, v in WATCH.items() if v is None]
        if missing:
            log.warning("entities not exposed by device: %s", missing)
        client.subscribe_states(_dispatch_state)

    async def on_disconnect(expected: bool) -> None:
        log.info("disconnected (expected=%s)", expected)

    reconnect = ReconnectLogic(
        client=client, on_connect=on_connect, on_disconnect=on_disconnect,
        zeroconf_instance=None, name=DEVICE_NAME,
    )
    await reconnect.start()
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await reconnect.stop()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
