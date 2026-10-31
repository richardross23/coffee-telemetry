#!/usr/bin/env python3
"""Match a Mahlkönig E65S GBW FRAM dump against the espresso shot archive
and backfill `ground_weight_g` + `grind_duration_s` into each matched shot.

Assumptions baked in from empirical probing of this specific grinder (fw 3.0,
serial 240216879):

  - The grinder's internal RTC does NOT track DST. Timestamps stored in FRAM
    are naive seconds that should be interpreted as fixed AEST (UTC+10)
    regardless of the real-world DST state. Interpreting them with the
    DST-aware "Australia/Melbourne" zone introduces a spurious ±60-min offset
    across the Oct DST transition.
  - The grinder's RTC also drifts a few minutes/month vs real AEST (see the
    per-month-offset diagnostic in the match-quality report). We absorb that
    by using a ±24h search window and chronological first-come-first-served
    pairing between shots and grinds, so within-session back-to-back shots
    can't both claim the same grind record.
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
from statistics import mean, stdev

UTC = datetime.timezone.utc
AEST = datetime.timezone(datetime.timedelta(hours=10))

SHOTS_DIR = Path("/volume2/docker/coffee/shots")
TS_FN = re.compile(r"^(\d{8}T\d{6}Z)_")

# FRAM layout (verified against firmware 3.0, build 2021-09-21)
REC_SIZE = 29
REGION_START = 0x1080
VALID_TS_LO = 1_577_836_800  # 2020-01-01
VALID_TS_HI = 2_000_000_000  # 2033-05
VALID_TARGET_G = (15.0, 25.0)
VALID_DELTA_G = 5.0           # drop records >5g off target
MATCH_WINDOW = datetime.timedelta(hours=24)


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


def match(shots: list[dict], grinds: list[dict]) -> list[tuple[dict, dict, int]]:
    """Nearest-in-time within ±24h, chronological FCFS. Returns list of
    (shot, grind, signed_gap_seconds) tuples. gap > 0 means grind precedes
    shot pump-on (normal); gap < 0 means grind record postdates shot
    (expected when the grinder's RTC runs slightly fast relative to AEST)."""
    claimed = [False] * len(grinds)
    out = []
    for s in shots:
        best_i, best_abs = -1, None
        for i, g in enumerate(grinds):
            if claimed[i]:
                continue
            d = abs(g["utc"] - s["pump_on_utc"])
            if d > MATCH_WINDOW:
                continue
            if best_abs is None or d < best_abs:
                best_i, best_abs = i, d
        if best_i < 0:
            continue
        claimed[best_i] = True
        gap_s = int((s["pump_on_utc"] - grinds[best_i]["utc"]).total_seconds())
        out.append((s, grinds[best_i], gap_s))
    return out


def apply_backfill(matches: list[tuple[dict, dict, int]], dry_run: bool) -> tuple[int, int, Path | None]:
    """Write canonical fields into each matched shot. Idempotent: skips
    files already carrying identical values. Returns (updated, unchanged,
    backup_dir)."""
    backup = None
    if not dry_run:
        stamp = datetime.datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = SHOTS_DIR / f"_grinder_sync_backup_{stamp}"
        backup.mkdir(exist_ok=True)
    updated = unchanged = 0
    for shot, grind, gap_s in matches:
        meta = shot["data"].setdefault("metadata", {})
        new_vals = {
            "ground_weight_g": grind["actual_g"],
            "grind_duration_s": grind["grind_s"],
            "grinder_raw_ts": grind["raw_ts"],
            "grinder_match_gap_s": gap_s,
        }
        # Drop legacy field names from previous iterations
        for legacy in ("grinder_dose_g", "grinder_grind_ms", "grinder_target_g"):
            meta.pop(legacy, None)
        if all(meta.get(k) == v for k, v in new_vals.items()):
            unchanged += 1
            continue
        meta.update(new_vals)
        if dry_run:
            updated += 1
            continue
        shutil.copy2(shot["path"], backup / shot["file"])
        tmp = shot["path"].with_suffix(".json.tmp")
        tmp.write_text(json.dumps(shot["data"], indent=2))
        os.replace(tmp, shot["path"])
        updated += 1
    return updated, unchanged, backup


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
    tight = sum(1 for g in gaps if abs(g) <= 900)
    lines.append(f"  high-confidence (|gap| ≤ 15min): {tight}/{len(gaps)}  ({tight/len(gaps)*100:.0f}%)")
    lines.append("  per-month median offset (grinder clock drift):")
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

    grinds = parse_fram(args.fram_bin)
    shots = load_shots()
    print(f"parsed:   {len(grinds):>4} valid grinder records "
          f"({grinds[0]['utc'].isoformat()} → {grinds[-1]['utc'].isoformat()})")
    print(f"loaded:   {len(shots):>4} espresso shots on disk")

    matches = match(shots, grinds)
    print(f"matched:  {len(matches):>4} shots ({len(shots) - len(matches)} unmatched, "
          f"{len(grinds) - len(matches)} orphan grinds)")

    updated, unchanged, backup = apply_backfill(matches, args.dry_run)
    print(f"applied:  {updated:>4} updated, {unchanged} already correct")
    if backup:
        print(f"backup:   {backup}")

    print("\nmatch quality:")
    print(match_quality_report(matches))

    if not args.dry_run and updated and not args.no_rebuild_index:
        print("\nrebuilding dashboard index (docker restart shot-logger)…")
        rebuild_index()
        print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
