#!/usr/bin/env python3
"""Import a Mahlkönig E65S GBW grinder log and backfill the matching shot
files with `ground_weight_g`, `grind_duration_s`, and `grinder_recipe`.

The grinder exposes a diagnostics Wi-Fi AP that yields rows like:

    Info, 2025/01/05 10:37:00, Recipe  1, ground, 20.0g, 20.1g, 5.903s, 0.000s

We only care about rows where state == 'ground' and the actual ground
weight is in espresso range. Each row is matched to the shot whose
`saved_at` falls within [grind_time + MIN_GAP, grind_time + MAX_GAP] and
is closest to the grind. Already-imported shots are skipped unless
`--force` is given.

Usage:
    # paste log into stdin
    pbpaste | python3 tools/import_grinder_log.py
    # or from a file
    python3 tools/import_grinder_log.py --log grinder.txt --apply

By default this prints a dry-run preview. Pass `--apply` to actually
publish via mosquitto_pub (run from the Mac, talks to the broker
container's exposed 1883).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_SHOTS_DIR = Path(__file__).resolve().parent.parent / "shots"
DEFAULT_TZ = "Australia/Melbourne"
# Realistic grind→pour gap. User reports 30s to 2min; allow some slack
# both ends (15s minimum to allow WDT+tamp; 5min maximum to absorb the
# odd interruption).
MIN_GAP_SEC = 15
MAX_GAP_SEC = 300
# Espresso-relevant ground weight range. Anything outside this is likely
# a purge / cleanup grind.
GROUND_RANGE_G = (15.0, 25.0)

LINE_RE = re.compile(
    r"""^\s*Info\s*,\s*
        (?P<ts>\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})\s*,\s*
        Recipe\s+(?P<recipe>\d+)\s*,\s*
        (?P<state>\w+)\s*,\s*
        (?P<target>[\d.]+)g\s*,\s*
        (?P<actual>[\d.]+)g\s*,\s*
        (?P<duration>[\d.]+)s\s*,\s*
        (?P<rest>[\d.]+)s\s*$""",
    re.VERBOSE,
)
SAVED_AT_RE = re.compile(r"^(\d{8}T\d{6}Z)_")


@dataclass
class GrindEvent:
    ts_utc: datetime.datetime
    recipe: int
    target_g: float
    actual_g: float
    duration_s: float


@dataclass
class ShotFile:
    path: Path
    saved_at_utc: datetime.datetime
    final_weight_g: float | None
    has_grind_metadata: bool


def parse_log(text: str, tz_name: str) -> list[GrindEvent]:
    tz = ZoneInfo(tz_name)
    events = []
    for line in text.splitlines():
        m = LINE_RE.match(line)
        if not m:
            continue
        if m.group("state") != "ground":
            continue
        actual = float(m.group("actual"))
        if not (GROUND_RANGE_G[0] <= actual <= GROUND_RANGE_G[1]):
            continue
        ts_local = datetime.datetime.strptime(m.group("ts"), "%Y/%m/%d %H:%M:%S").replace(tzinfo=tz)
        events.append(GrindEvent(
            ts_utc=ts_local.astimezone(datetime.timezone.utc),
            recipe=int(m.group("recipe")),
            target_g=float(m.group("target")),
            actual_g=actual,
            duration_s=float(m.group("duration")),
        ))
    return events


def load_shots(shots_dir: Path) -> list[ShotFile]:
    shots = []
    for path in sorted(shots_dir.glob("*.json")):
        if path.name == "index.json":
            continue
        m = SAVED_AT_RE.match(path.name)
        if not m:
            continue
        s = m.group(1)
        saved_at = datetime.datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.timezone.utc)
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        meta = data.get("metadata") or {}
        shots.append(ShotFile(
            path=path,
            saved_at_utc=saved_at,
            final_weight_g=data.get("final_weight_g"),
            has_grind_metadata="ground_weight_g" in meta,
        ))
    return shots


def match(events: list[GrindEvent], shots: list[ShotFile]) -> tuple[list[tuple[GrindEvent, ShotFile, int]], list[GrindEvent], list[ShotFile]]:
    """Return (matches, unmatched_grinds, unmatched_shots).

    Each match is (grind, shot, gap_seconds) where gap is shot.saved_at - grind.ts.
    Greedy nearest: for each grind in time order, take the closest unmatched
    shot in the [MIN_GAP, MAX_GAP] window.
    """
    used_shots: set[Path] = set()
    matches = []
    unmatched_g = []
    for g in sorted(events, key=lambda e: e.ts_utc):
        candidates = []
        for s in shots:
            if s.path in used_shots:
                continue
            gap = (s.saved_at_utc - g.ts_utc).total_seconds()
            if MIN_GAP_SEC <= gap <= MAX_GAP_SEC:
                candidates.append((gap, s))
        if not candidates:
            unmatched_g.append(g)
            continue
        candidates.sort(key=lambda c: c[0])
        gap, shot = candidates[0]
        matches.append((g, shot, int(gap)))
        used_shots.add(shot.path)
    unmatched_s = [s for s in shots if s.path not in used_shots]
    return matches, unmatched_g, unmatched_s


def fmt_local(dt_utc: datetime.datetime, tz_name: str) -> str:
    return dt_utc.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M:%S")


def publish(file_basename: str, fields: dict, *, broker_container: str = "mosquitto") -> None:
    payload = json.dumps({"file": file_basename, "metadata": fields})
    subprocess.run(
        ["docker", "exec", broker_container,
         "mosquitto_pub", "-h", "localhost",
         "-t", "coffee/shot/metadata/set", "-m", payload],
        check=True,
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--log", type=Path, help="Grinder log file (default: stdin)")
    p.add_argument("--shots-dir", type=Path, default=DEFAULT_SHOTS_DIR, help=f"(default: {DEFAULT_SHOTS_DIR})")
    p.add_argument("--tz", default=DEFAULT_TZ, help=f"Grinder local timezone (default: {DEFAULT_TZ})")
    p.add_argument("--apply", action="store_true", help="Publish updates via mosquitto_pub. Without this, dry-run only.")
    p.add_argument("--force", action="store_true", help="Overwrite shots that already have grind metadata.")
    p.add_argument("--broker-container", default="mosquitto", help="(default: mosquitto)")
    args = p.parse_args()

    if args.log:
        text = args.log.read_text()
    else:
        if sys.stdin.isatty():
            print("paste log lines, then Ctrl-D:", file=sys.stderr)
        text = sys.stdin.read()

    events = parse_log(text, args.tz)
    if not events:
        print("no parseable 'ground' rows in input", file=sys.stderr)
        return 1
    shots = load_shots(args.shots_dir)
    if not shots:
        print(f"no shot files under {args.shots_dir}", file=sys.stderr)
        return 1

    matches, unmatched_g, unmatched_s = match(events, shots)

    print(f"\n{len(events)} grind event(s) parsed, {len(shots)} shot(s) on disk\n")
    print(f"--- {len(matches)} match(es) ---")
    for g, s, gap in matches:
        skip = " [SKIP — already has grind metadata, use --force]" if s.has_grind_metadata and not args.force else ""
        print(
            f"  {fmt_local(g.ts_utc, args.tz)}  recipe {g.recipe}  "
            f"ground {g.actual_g:.1f}g in {g.duration_s:.2f}s   "
            f"→  shot {fmt_local(s.saved_at_utc, args.tz)} ({s.final_weight_g}g out)   "
            f"+{gap}s{skip}"
        )

    if unmatched_g:
        print(f"\n--- {len(unmatched_g)} unmatched grind(s) ---")
        for g in unmatched_g:
            print(f"  {fmt_local(g.ts_utc, args.tz)}  ground {g.actual_g:.1f}g (no shot in {MIN_GAP_SEC}-{MAX_GAP_SEC}s window)")
    if unmatched_s:
        print(f"\n--- {len(unmatched_s)} unmatched shot(s) ---")
        for s in unmatched_s:
            print(f"  {fmt_local(s.saved_at_utc, args.tz)} ({s.final_weight_g}g out)")

    if not args.apply:
        print("\n(dry run — pass --apply to publish)")
        return 0

    applied = 0
    skipped = 0
    for g, s, _gap in matches:
        if s.has_grind_metadata and not args.force:
            skipped += 1
            continue
        publish(s.path.name, {
            "ground_weight_g": g.actual_g,
            "grind_duration_s": g.duration_s,
            "grinder_recipe": f"Recipe {g.recipe}",
        }, broker_container=args.broker_container)
        applied += 1
    print(f"\napplied {applied}, skipped {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
