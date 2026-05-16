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

DEVICE_HOST = os.environ.get("DEVICE_HOST", "192.168.2.29")
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

    if _prev_shot_state == "IDLE" and new_state == "PREINFUSION":
        # Start of a new shot — open the buffer with 10 s of prepended scale.
        start = time.time()
        cutoff = start - PREPEND_LOOKBACK_SEC
        prepend = [
            {"t_ms": int((ts - start) * 1000), "weight_g": w, "flow_g_s": 0.0}
            for ts, w in _scale_buffer
            if ts >= cutoff and abs(w) <= PREPEND_MAX_WEIGHT_G
        ]
        _shot = ShotInFlight(start_wall=start, prepend=prepend)
        log.info("shot started (%d prepend samples)", len(prepend))

    elif new_state == "DONE":
        _maybe_finalize()

    elif new_state == "IDLE" and _shot is not None:
        # Belt-and-braces: clear if state machine returns to IDLE without DONE
        # (shouldn't happen with current firmware but stays defensive).
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
    if not isinstance(value, str) or not value.strip(): return
    try:
        summary = json.loads(value)
    except json.JSONDecodeError as e:
        log.warning("shot_summary: bad JSON: %s", e)
        return
    if _shot is None:
        log.warning("shot_summary arrived with no in-flight shot")
        return
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
