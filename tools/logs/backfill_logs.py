#!/usr/bin/env python3
"""Walk the shot archive and attach bag + grind-setting metadata by
inheriting forward from append-only event logs:

  shots/bag_log.json   [{ts_utc, bean, roast_date, note?}, …]
  shots/grind_log.json [{ts_utc, setting, note?}, …]

For each timestamped shot file, assign the most recent prior entry from
each log. Writes to the dashboard's existing metadata schema:
  metadata.bean_type    ← bag entry's `bean`
  metadata.roast_date   ← bag entry's `roast_date`
  metadata.grinder_setting ← grind entry's `setting`

The dashboard computes days-off-roast on the fly from roast_date, so we
don't duplicate it here. Idempotent, atomic, rebuilds the dashboard
index via `docker restart shot-logger` only when something changed.

Called automatically from bag_log.py and grind_log.py after each append.
Can also be run standalone, e.g. after hand-editing a log file.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

UTC = datetime.timezone.utc

SHOTS_DIR = Path("/volume2/docker/coffee/shots")
BAG_LOG = SHOTS_DIR / "bag_log.json"
GRIND_LOG = SHOTS_DIR / "grind_log.json"
TS_FN = re.compile(r"^(\d{8}T\d{6}Z)_")


def load_log(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    for e in data:
        e["_ts"] = datetime.datetime.fromisoformat(e["ts_utc"].replace("Z", "+00:00"))
    return sorted(data, key=lambda e: e["_ts"])


def latest_at(log: list[dict], when: datetime.datetime) -> dict | None:
    best = None
    for e in log:
        if e["_ts"] <= when:
            best = e
        else:
            break
    return best


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


def apply(shots: list[dict], bags: list[dict], grinds: list[dict],
          dry_run: bool) -> tuple[int, int, Path | None]:
    backup = None
    if not dry_run:
        stamp = datetime.datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = SHOTS_DIR / f"_logs_backfill_backup_{stamp}"
    updated = unchanged = 0
    for shot in shots:
        bag = latest_at(bags, shot["pump_on_utc"])
        grind = latest_at(grinds, shot["pump_on_utc"])
        new_vals: dict[str, object] = {}
        if bag:
            new_vals["bean_type"] = bag["bean"]
            new_vals["roast_date"] = bag["roast_date"]
        if grind:
            new_vals["grinder_setting"] = grind["setting"]
        if not new_vals:
            continue
        meta = shot["data"].setdefault("metadata", {})
        if all(meta.get(k) == v for k, v in new_vals.items()):
            unchanged += 1
            continue
        meta.update(new_vals)
        if dry_run:
            updated += 1
            continue
        if backup and not backup.exists():
            backup.mkdir(exist_ok=True)
        shutil.copy2(shot["path"], backup / shot["file"])
        tmp = shot["path"].with_suffix(".json.tmp")
        tmp.write_text(json.dumps(shot["data"], indent=2))
        os.replace(tmp, shot["path"])
        updated += 1
    return updated, unchanged, backup if backup and backup.exists() else None


def rebuild_index() -> None:
    subprocess.run(["docker", "restart", "shot-logger"], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-rebuild-index", action="store_true")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress diagnostic output (used by one-shot callers)")
    args = p.parse_args()

    bags = load_log(BAG_LOG)
    grinds = load_log(GRIND_LOG)
    shots = load_shots()

    def say(msg: str) -> None:
        if not args.quiet:
            print(msg)

    say(f"loaded: {len(shots):>4} shots, {len(bags)} bag entries, {len(grinds)} grind entries")
    updated, unchanged, backup = apply(shots, bags, grinds, args.dry_run)
    say(f"applied: {updated} updated, {unchanged} already correct")
    if backup:
        say(f"backup: {backup}")

    if not args.dry_run and updated and not args.no_rebuild_index:
        say("rebuilding dashboard index (docker restart shot-logger)…")
        rebuild_index()
        say("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
