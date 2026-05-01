#!/usr/bin/env python3
"""Backfill old-firmware shots so all timestamps are in the pump_on=0 frame.

Under the original firmware, shot_start fired at first weight rise (~first
drop), so sample t_ms and pump_off_at_s were relative to first_drop.
Under the pump-driven firmware (current), shot_start fires at pump_on, so
all timestamps are relative to pump_on.

This script detects old-frame shots (first_drop_t close to 0 with dwell
present) and shifts every timestamp by +dwell_ms, putting all shots into
the new convention. After running, all shots with accelerometer data have
pump_on at t=0, first_drop at t=dwell, etc.

Idempotent — re-running on already-migrated shots is a no-op.

Usage:  python3 tools/backfill_pump_on_frame.py
"""

import argparse
import json
import os
import sys
from glob import glob


def first_drop_t_ms(samples, threshold=0.1):
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


def is_old_frame(samples, dwell_s):
    if dwell_s is None or not samples:
        return False
    fd = first_drop_t_ms(samples)
    if fd is None:
        return False
    # New frame: first_drop ≈ +dwell_ms; old frame: first_drop ≈ 0 (or
    # negative if prepend buffer is included). Anything below dwell/2 is
    # almost certainly old-frame.
    return fd < (dwell_s * 1000) / 2.0


def migrate(d):
    samples = d.get("samples") or []
    dwell_s = d["dwell_s"]
    dwell_ms = int(dwell_s * 1000)
    for s in samples:
        if s.get("t_ms") is not None:
            s["t_ms"] = s["t_ms"] + dwell_ms
    for k in ("pump_off_at_s", "last_drop_at_s",
              "acaia_start_at_s", "acaia_stop_at_s"):
        if d.get(k) is not None:
            d[k] = d[k] + dwell_s
    d["_pump_on_frame_migrated"] = True


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--shots-dir", default="/Users/rwross/docker/coffee-telemetry/shots")
    p.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = p.parse_args()

    migrated, skipped_new, skipped_no_dwell = [], [], []
    for path in sorted(glob(os.path.join(args.shots_dir, "*.json"))):
        name = os.path.basename(path)
        if name == "index.json":
            continue
        with open(path) as f:
            d = json.load(f)
        samples = d.get("samples") or []
        dwell_s = d.get("dwell_s")

        if dwell_s is None:
            skipped_no_dwell.append(name)
            continue
        if d.get("_pump_on_frame_migrated"):
            skipped_new.append(name + " (already migrated)")
            continue
        if not is_old_frame(samples, dwell_s):
            skipped_new.append(name)
            continue

        before_fd = first_drop_t_ms(samples)
        before_poff = d.get("pump_off_at_s")
        migrate(d)
        after_fd = first_drop_t_ms(d["samples"])
        after_poff = d.get("pump_off_at_s")
        print(f"  {name}: first_drop {before_fd} → {after_fd} ms; "
              f"pump_off {before_poff} → {after_poff} s")

        if not args.dry_run:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(d, f, indent=2)
            os.replace(tmp, path)
        migrated.append(name)

    print()
    print(f"migrated:                {len(migrated)}")
    print(f"skipped (already new):   {len(skipped_new)}")
    print(f"skipped (no dwell data): {len(skipped_no_dwell)}")
    if args.dry_run:
        print("\n(dry run — no files written. Re-run without --dry-run to apply.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
