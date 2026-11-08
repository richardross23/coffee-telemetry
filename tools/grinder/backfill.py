#!/usr/bin/env python3
"""Match a Mahlkönig E65S GBW FRAM dump against the espresso shot archive
and backfill `ground_weight_g` + `grind_duration_s` into each matched shot.
Where the grinder recorded a disc distance (patched firmware, see
GRINDER-FIRMWARE-PATCH.md) it also sets `grinder_setting` to that value in µm.

Assumptions baked in from empirical probing of this specific grinder (fw 3.0,
serial 240216879):

  - FRAM timestamps are the grinder clock's naive wall time. The clock doesn't
    switch for DST on its own, drifts a few minutes/month, and gets reset by
    hand, so its offset from real time isn't fixed. parse_fram reads it as
    AEST (UTC+10); match() then estimates the remaining offset per day from
    the data and pairs each shot with the grind made just before it. A grind
    it can't place (e.g. a 0 g record dropped below) leaves its shot
    unmatched rather than shifting later shots onto the wrong grinds.
  - Valid espresso grinds are target ∈ [15, 25]g AND |actual-target| ≤ 5g.
    Anything outside is a calibration test / purge / junk and is dropped.
  - Only timestamped shot files (coffee-telemetry wallclock+shot_id convention)
    are considered; bare `<shot_id>.json` raw archives are ignored.

Writes atomic, with a per-run backup directory. Rebuilds the dashboard
index via `docker restart shot-logger` so the dashboard sees the new
values on next load.

Usage:
    python3 backfill.py --fram-bin /tmp/fram.bin           # apply
    python3 backfill.py --fram-bin /tmp/fram.bin --dry-run # preview only
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from statistics import mean, median, stdev

UTC = datetime.timezone.utc
AEST = datetime.timezone(datetime.timedelta(hours=10))

SHOTS_DIR = Path("/volume2/docker/coffee/shots")
TS_FN = re.compile(r"^(\d{8}T\d{6}Z)_")

# FRAM layout (verified against firmware 3.0, build 2021-09-21)
REC_SIZE = 29
REGION_START = 0x1080
REGION_END = 0x9080           # end of the shot log; shorter = partial download
VALID_TS_LO = 1_577_836_800  # 2020-01-01
VALID_TS_HI = 2_000_000_000  # 2033-05
VALID_TARGET_G = (15.0, 25.0)
VALID_DELTA_G = 5.0           # drop records >5g off target

# Matching. Gap = shot pump-on − grind time as parse_fram reads it.
OFFSET_SEARCH = datetime.timedelta(hours=3)    # largest clock error considered
OFFSET_AGREE = datetime.timedelta(minutes=2)   # gaps this close are one offset
# Corrected gap (gap − day's offset) a pair must fall in: the grind can be up
# to 15 min earlier than usual before its shot, and only slightly later.
PAIR_WINDOW = (datetime.timedelta(minutes=-5), datetime.timedelta(minutes=15))

# Written by apply_backfill and removed again if the shot loses its match.
# The logger never inherits grinder_raw_ts, so its presence marks a shot
# whose dose and grind time came from here.
BACKFILLED_FIELDS = ("ground_weight_g", "grind_duration_s",
                     "grinder_raw_ts", "grinder_match_gap_s")


def parse_fram(path: Path) -> list[dict]:
    buf = path.read_bytes()
    records = []
    for off in range(REGION_START, len(buf) - REC_SIZE, REC_SIZE):
        rec = buf[off:off + REC_SIZE]
        ts = struct.unpack("<I", rec[5:9])[0]
        if not (VALID_TS_LO < ts < VALID_TS_HI):
            break  # end-of-log sentinel
        target = struct.unpack("<H", rec[9:11])[0] / 100.0
        actual = struct.unpack("<H", rec[13:15])[0] / 100.0
        grind_ms = struct.unpack("<H", rec[21:23])[0]
        # Patched firmware (2026-11-07) stores disc distance in µm here;
        # 0 = not recorded (stock firmware, or pre-patch record).
        ddd_um = struct.unpack("<i", rec[17:21])[0] or None
        if not (VALID_TARGET_G[0] <= target <= VALID_TARGET_G[1]):
            continue
        if abs(actual - target) > VALID_DELTA_G:
            continue
        # Interpret naive stored seconds as AEST (DST-ignoring)
        naive = datetime.datetime.fromtimestamp(ts, tz=UTC).replace(tzinfo=None)
        utc_dt = naive.replace(tzinfo=AEST).astimezone(UTC)
        records.append({
            "utc": utc_dt,
            "actual_g": round(actual, 2),
            "target_g": round(target, 2),
            "grind_s": round(grind_ms / 1000.0, 3),
            "raw_ts": ts,
            "ddd_um": ddd_um,
        })
    return sorted(records, key=lambda r: r["utc"])


def load_shots() -> list[dict]:
    shots = []
    for fn in sorted(os.listdir(SHOTS_DIR)):
        m = TS_FN.match(fn)
        if not m:
            continue
        wall = datetime.datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        path = SHOTS_DIR / fn
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        dur = data.get("duration_s") or 30
        shots.append({
            "file": fn,
            "path": path,
            "pump_on_utc": wall - datetime.timedelta(seconds=dur),
            "data": data,
        })
    return sorted(shots, key=lambda s: s["pump_on_utc"])


def shot_day(shot: dict) -> datetime.date:
    return shot["pump_on_utc"].astimezone(AEST).date()


def estimate_offsets(shots: list[dict], grinds: list[dict]) -> dict[datetime.date, datetime.timedelta]:
    """Per day, the gap that most of that day's shots share with some grind.
    That's the grinder clock's offset plus the usual grind-to-pump delay, and
    it absorbs DST, drift and resets. A day needs two shots agreeing; any
    other day borrows the nearest such day's offset (earlier on a tie)."""
    gaps_by_day: dict[datetime.date, list[list[datetime.timedelta]]] = {}
    for s in shots:
        gaps = [s["pump_on_utc"] - g["utc"] for g in grinds
                if abs(s["pump_on_utc"] - g["utc"]) <= OFFSET_SEARCH]
        gaps_by_day.setdefault(shot_day(s), []).append(gaps)

    def agrees(gaps, c):
        return any(abs(g - c) <= OFFSET_AGREE for g in gaps)

    confident = {}
    for day, per_shot in gaps_by_day.items():
        best, votes = None, 0
        for c in (g for gaps in per_shot for g in gaps):
            n = sum(1 for gaps in per_shot if agrees(gaps, c))
            if n > votes:
                best, votes = c, n
        if votes >= 2:
            confident[day] = median(min(gaps, key=lambda g: abs(g - best))
                                    for gaps in per_shot if agrees(gaps, best))

    offsets = {}
    for day in gaps_by_day:
        if day in confident:
            offsets[day] = confident[day]
        elif confident:
            nearest = min(confident, key=lambda d: (abs((d - day).days), d > day))
            offsets[day] = confident[nearest]
        else:
            offsets[day] = datetime.timedelta(0)
    return offsets


def match(shots: list[dict], grinds: list[dict]) -> list[tuple[dict, dict, int]]:
    """Pair each shot with the grind made just before it. Grind times are
    corrected by the day's estimated offset; a pair counts only if its
    corrected gap is inside PAIR_WINDOW, and pairs are taken closest-first.
    Returns (shot, grind, signed_gap_seconds) in shot order; the gap is the
    raw one (pump-on − grind as read), so it still shows the clock's offset."""
    offsets = estimate_offsets(shots, grinds)
    candidates = []
    for si, s in enumerate(shots):
        offset = offsets[shot_day(s)]
        for gi, g in enumerate(grinds):
            corrected = s["pump_on_utc"] - g["utc"] - offset
            if PAIR_WINDOW[0] <= corrected <= PAIR_WINDOW[1]:
                candidates.append((abs(corrected), si, gi))
    candidates.sort()
    shot_taken, grind_taken, out = set(), set(), []
    for _, si, gi in candidates:
        if si in shot_taken or gi in grind_taken:
            continue
        shot_taken.add(si)
        grind_taken.add(gi)
        gap_s = int((shots[si]["pump_on_utc"] - grinds[gi]["utc"]).total_seconds())
        out.append((shots[si], grinds[gi], gap_s))
    return sorted(out, key=lambda m: m[0]["pump_on_utc"])


def write_shot(shot: dict, backup: Path) -> None:
    shutil.copy2(shot["path"], backup / shot["file"])
    tmp = shot["path"].with_suffix(".json.tmp")
    tmp.write_text(json.dumps(shot["data"], indent=2))
    os.replace(tmp, shot["path"])


def apply_backfill(matches: list[tuple[dict, dict, int]], dry_run: bool,
                   unmatched: list[dict] = ()) -> tuple[int, int, int, Path | None]:
    """Write canonical fields into each matched shot, and remove them from
    unmatched shots that carry them from an earlier run. Idempotent: skips
    files already carrying identical values. Returns (updated, unchanged,
    cleared, backup_dir)."""
    backup = None
    if not dry_run:
        stamp = datetime.datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = SHOTS_DIR / f"_grinder_sync_backup_{stamp}"
        backup.mkdir(exist_ok=True)
    updated = unchanged = cleared = 0
    for shot in unmatched:
        meta = shot["data"].get("metadata", {})
        if "grinder_raw_ts" not in meta:
            continue
        for k in BACKFILLED_FIELDS:
            meta.pop(k, None)
        if not dry_run:
            write_shot(shot, backup)
        cleared += 1
    for shot, grind, gap_s in matches:
        meta = shot["data"].setdefault("metadata", {})
        new_vals = {
            "ground_weight_g": grind["actual_g"],
            "grind_duration_s": grind["grind_s"],
            "grinder_raw_ts": grind["raw_ts"],
            "grinder_match_gap_s": gap_s,
        }
        if grind["ddd_um"] is not None:
            # Measured disc distance replaces the hand-entered sticky value
            # (same unit: µm, as the Grind Change Shortcut records it).
            new_vals["grinder_setting"] = grind["ddd_um"]
        # Drop legacy field names from previous iterations
        for legacy in ("grinder_dose_g", "grinder_grind_ms", "grinder_target_g"):
            meta.pop(legacy, None)
        if all(meta.get(k) == v for k, v in new_vals.items()):
            unchanged += 1
            continue
        meta.update(new_vals)
        if not dry_run:
            write_shot(shot, backup)
        updated += 1
    return updated, unchanged, cleared, backup


def rebuild_index():
    """Fastest reliable way to rebuild shots/index.json with the new
    metadata is to bounce the shot-logger container (its startup runs
    index_scan). ~3s."""
    subprocess.run(["docker", "restart", "shot-logger"], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def match_quality_report(matches: list[tuple[dict, dict, int]]) -> str:
    if not matches:
        return "  (no matches)"
    gaps = [g for _, _, g in matches]
    by_month: dict[str, list[int]] = {}
    for s, _, g in matches:
        key = s["pump_on_utc"].strftime("%Y-%m")
        by_month.setdefault(key, []).append(g)
    lines = []
    lines.append(f"  overall:  n={len(gaps)}  median_gap={sorted(gaps)[len(gaps)//2]:+d}s  "
                 f"sd={stdev(gaps) if len(gaps) > 1 else 0:.0f}s")
    lines.append("  per-month median raw gap (grinder clock offset + grind-to-pump delay):")
    for k in sorted(by_month):
        xs = sorted(by_month[k])
        lines.append(f"    {k}  n={len(xs):3}  median={xs[len(xs)//2]:+5d}s")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--fram-bin", type=Path, required=True,
                   help="Path to a fram.bin produced by fetch_fram.py or coffee-fram-sync")
    p.add_argument("--dry-run", action="store_true",
                   help="Report what would change without writing")
    p.add_argument("--no-rebuild-index", action="store_true",
                   help="Skip the shot-logger restart (useful for testing)")
    args = p.parse_args()

    if not args.fram_bin.is_file():
        print(f"not a file: {args.fram_bin}", file=sys.stderr)
        return 1

    # A partial file holds only the oldest records, so every newer shot would
    # look unmatched and lose its backfilled values.
    size = args.fram_bin.stat().st_size
    if size < REGION_END:
        print(f"partial FRAM dump: {size} bytes, shot log ends at {REGION_END}",
              file=sys.stderr)
        return 2

    grinds = parse_fram(args.fram_bin)
    shots = load_shots()
    print(f"parsed:   {len(grinds):>4} valid grinder records "
          f"({grinds[0]['utc'].isoformat()} → {grinds[-1]['utc'].isoformat()})")
    print(f"loaded:   {len(shots):>4} espresso shots on disk")

    matches = match(shots, grinds)
    print(f"matched:  {len(matches):>4} shots ({len(shots) - len(matches)} unmatched, "
          f"{len(grinds) - len(matches)} orphan grinds)")

    matched_ids = {id(s) for s, _, _ in matches}
    unmatched = [s for s in shots if id(s) not in matched_ids]
    updated, unchanged, cleared, backup = apply_backfill(matches, args.dry_run, unmatched)
    print(f"applied:  {updated:>4} updated, {unchanged} already correct, "
          f"{cleared} cleared (lost their match)")
    if backup:
        print(f"backup:   {backup}")

    print("\nmatch quality:")
    print(match_quality_report(matches))

    if not args.dry_run and (updated or cleared) and not args.no_rebuild_index:
        print("\nrebuilding dashboard index (docker restart shot-logger)…")
        rebuild_index()
        print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
