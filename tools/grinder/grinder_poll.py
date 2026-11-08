#!/usr/bin/env python3
"""Watch the grinder through the morning and report each grind.

Runs on the Mac, the one machine on both the LAN and the grinder's Wi-Fi. Reads
/stats (needs the laststat firmware patch) and publishes each new grind to
coffee/grinder/last, where shot-logger holds it for the next shot. A grind
usually comes 1-2 minutes before its shot, so matching happens in shot-logger
when the shot ends, not here.

When the window ends it checks the morning's shots, runs coffee-fram-sync if any
missed its grind, and publishes a report that Home Assistant pushes to the phone.

launchd starts it at 06:30 and at login (com.rwross.coffee-grinder-poll.plist).
The espresso machine's smart switch turns off at 11:00; the extra five minutes
lets a shot pulled just before then get saved.

Usage: grinder_poll.py [--end HH:MM]
"""

from __future__ import annotations

import argparse
import calendar
import http.client
import json
import re
import subprocess
import sys
import time
import urllib.request

GRINDER = "http://192.168.4.1"
BROKER = "192.168.1.10"
SHOT_INDEX = f"http://{BROKER}:8418/shots/index.json"
WIFI_IF = "en1"
GRINDER_SSID = "E65GbW240216879-240216879"

WINDOW_START = "06:30"
WINDOW_END = "11:05"
POLL_S = 10               # a grind is 1-2 minutes ahead of its shot
GAP_POLLS = 3             # this many misses in a row counts as an outage
JOIN_EVERY_S = 60
GRIND_LOOKBACK_S = 15 * 60  # as in shot-logger

T_LAST = "coffee/grinder/last"
T_REPORT = "coffee/grinder/report"
T_DISCOVERY = "homeassistant/sensor/coffee_grinder_report/config"
DISCOVERY = {
    "name": "Coffee Grinder Report",
    "unique_id": "coffee_grinder_report",
    "state_topic": T_REPORT,
    "value_template": "{{ value_json.date }}",
    "json_attributes_topic": T_REPORT,
    "icon": "mdi:coffee",
}

LAST_FIELD = re.compile(r'"(last\w+)":"(-?\d+)"')
LAST_FIELDS = ("lastTs", "lastTg", "lastAc", "lastDisc", "lastGrnd", "lastFlag")
HTTP_ERRORS = (OSError, http.client.HTTPException, ValueError)


def say(msg: str) -> None:
    print(f"{time.strftime('%F %T')} {msg}", flush=True)


def parse_stats(body: str) -> dict | None:
    """The last-shot fields, or None. /stats is cut off at about 1 KB, so it's
    not valid JSON."""
    fields = {k: int(v) for k, v in LAST_FIELD.findall(body)}
    return fields if all(k in fields for k in LAST_FIELDS) else None


class Watcher:
    """Turns successive /stats readings into grinds and outages."""

    def __init__(self):
        self.last_ts = None     # None: next reading is only a baseline
        self.misses = 0
        self.first_miss = 0.0
        self.outages: list[tuple[float, float]] = []
        self.grinds = 0

    def step(self, fields: dict | None, now: float) -> dict | None:
        """Take one reading (None if the grinder didn't answer). Returns a
        grind to publish, stamped with the time it was first seen."""
        if fields is None:
            if not self.misses:
                self.first_miss = now
            self.misses += 1
            return None
        if self.misses >= GAP_POLLS:
            self.outages.append((self.first_miss, now))
            self.last_ts = None   # a grind made during the outage can't be timed
        self.misses = 0

        ts = fields["lastTs"]
        if self.last_ts is None:
            self.last_ts = ts
            return None
        if not ts or ts == self.last_ts:   # lastTs reads 0 during a grind
            return None
        self.last_ts = ts
        return {"ts": ts, "target_cg": fields["lastTg"], "actual_cg": fields["lastAc"],
                "disc_um": fields["lastDisc"], "grind_ms": fields["lastGrnd"],
                "flag": fields["lastFlag"], "seen_at": now}

    def close(self, end: float) -> None:
        if self.misses >= GAP_POLLS:
            self.outages.append((self.first_miss, end))


def shots_in_window(index: list[dict], start: float, end: float) -> list[dict]:
    """Saved shots whose pump started in the window, oldest first."""
    shots = []
    for entry in index:
        try:
            saved = calendar.timegm(time.strptime(entry["saved_at"], "%Y-%m-%dT%H:%M:%SZ"))
        except (KeyError, TypeError, ValueError):
            continue
        begin = saved - (entry.get("duration_s") or 30)
        if start <= begin <= end:
            shots.append({"file": entry.get("file"), "start": begin,
                          "has_grinder": "grinder_raw_ts" in (entry.get("metadata") or {})})
    return sorted(shots, key=lambda s: s["start"])


def missing(shots: list[dict]) -> list[dict]:
    return [s for s in shots if not s["has_grinder"]]


def hhmm(t: float) -> str:
    return time.strftime("%H:%M", time.localtime(t))


def build_report(day: str, shots: list[dict], grinds: int, outages: list[tuple[float, float]],
                 sync: str, filled: int) -> dict:
    """The morning's result. An empty message means nothing worth a push: with
    no shots, a grinder that never answered was simply switched off."""
    n, lost = len(shots), missing(shots)
    if not n:
        message = ""
    elif lost:
        message = f"{len(lost)} of {n} shots missing grinder data."
    elif n == 1:
        message = "1 shot, with grinder data."
    else:
        message = f"{n} shots, all with grinder data."

    if n and filled:
        message += f" {filled} needed the FRAM sync."
    if n and sync == "failed":
        message += " FRAM sync failed."

    affected = lost or (shots if filled else [])
    gaps = [(a, b) for a, b in outages
            if any(a <= s["start"] and b >= s["start"] - GRIND_LOOKBACK_S for s in affected)]
    if gaps:
        message += " Mac lost the grinder " + ", ".join(f"{hhmm(a)}-{hhmm(b)}" for a, b in gaps) + "."

    return {"date": day, "shots": n, "missing": len(lost), "grinds_seen": grinds,
            "fram_sync": sync, "outages": [[hhmm(a), hhmm(b)] for a, b in outages],
            "message": message}


def fetch(url: str, timeout: float) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode("latin-1")
    except HTTP_ERRORS:
        return None


def read_stats() -> dict | None:
    body = fetch(f"{GRINDER}/stats", 3)
    return parse_stats(body) if body else None


def read_shots(start: float, end: float) -> list[dict] | None:
    body = fetch(SHOT_INDEX, 10)
    try:
        return shots_in_window(json.loads(body), start, end) if body else None
    except ValueError:
        return None


def run(cmd: list[str], timeout: float) -> bool:
    try:
        return subprocess.run(cmd, timeout=timeout, stdout=sys.stdout,
                              stderr=sys.stdout).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def publish(topic: str, payload: dict) -> bool:
    return run(["mosquitto_pub", "-h", BROKER, "-t", topic, "-q", "1", "-r",
                "-m", json.dumps(payload)], 10)


def watch(end: float) -> Watcher:
    watcher, pending, last_join = Watcher(), None, 0.0
    while (now := time.time()) < end:
        was_down = watcher.misses >= GAP_POLLS
        grind = watcher.step(read_stats(), now)
        if grind:
            pending = grind
        if pending and publish(T_LAST, pending):
            say(f"grind {pending['ts']}: {pending['actual_cg'] / 100:.2f} g")
            watcher.grinds += 1
            pending = None

        down = watcher.misses >= GAP_POLLS
        if down != was_down:
            say("grinder not answering" if down else "grinder answering again")
        if down and now - last_join >= JOIN_EVERY_S:
            run(["networksetup", "-setairportnetwork", WIFI_IF, GRINDER_SSID], 30)
            last_join = now
        time.sleep(max(0.0, POLL_S - (time.time() - now)))
    watcher.close(end)
    return watcher


def fill_missing(shots: list[dict], start: float, end: float) -> tuple[list[dict], str, int]:
    """Run the full FRAM sync once for shots that missed their grind."""
    before = len(missing(shots))
    say(f"{before} shot(s) without grinder data, running coffee-fram-sync")
    sync = "ok" if run(["coffee-fram-sync"], 300) else "failed"
    deadline = time.time() + 30    # backfill restarts shot-logger, which rewrites the index
    while True:
        latest = read_shots(start, end)
        if latest is not None:
            shots = latest
        if not missing(shots) or time.time() >= deadline:
            return shots, sync, before - len(missing(shots))
        time.sleep(2)


def clock(hm: str) -> float:
    """Today's HH:MM as an epoch time."""
    return time.mktime(time.strptime(time.strftime("%Y-%m-%d ") + hm, "%Y-%m-%d %H:%M"))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--end", default=WINDOW_END, help="end of the window, HH:MM")
    args = p.parse_args()

    start, end = clock(WINDOW_START), clock(args.end)
    if not start <= time.time() < end:
        return 0
    say(f"watching until {args.end}")
    publish(T_DISCOVERY, DISCOVERY)
    watcher = watch(end)

    shots, sync, filled = read_shots(start, end), "not needed", 0
    if shots and missing(shots):
        shots, sync, filled = fill_missing(shots, start, end)
    report = build_report(time.strftime("%Y-%m-%d"), shots or [], watcher.grinds,
                          watcher.outages, sync, filled)
    if shots is None:
        report["message"] = "Couldn't read this morning's shots."
    say(f"report: {json.dumps(report)}")
    if not publish(T_REPORT, report):
        say("could not publish the report")
    return 0


if __name__ == "__main__":
    sys.exit(main())
