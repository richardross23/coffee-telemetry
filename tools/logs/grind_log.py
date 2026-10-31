#!/usr/bin/env python3
"""Record a grinder-setting change and backfill the shot archive.

Called from the Mac/phone via SSH; typical invocation:

    ssh refuge python3 /volume2/docker/coffee/tools/logs/grind_log.py \
        122 --note "went coarser"

Appends one entry to shots/grind_log.json and immediately runs
backfill_logs.py so the dashboard reflects the new setting on next load.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

UTC = datetime.timezone.utc
GRIND_LOG = Path("/volume2/docker/coffee/shots/grind_log.json")
BACKFILL = Path(__file__).with_name("backfill_logs.py")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("setting", type=float,
                   help="Grinder dial position (number; decimals OK)")
    p.add_argument("--note", default="",
                   help="Optional free-text note (e.g. 'went coarser, new bag')")
    p.add_argument("--ts", default=None,
                   help="Override timestamp (ISO 8601, UTC). Default: now.")
    p.add_argument("--no-backfill", action="store_true",
                   help="Append the entry but skip the backfill run")
    args = p.parse_args()

    # Normalise ints vs floats so 122 doesn't come back as 122.0
    setting = int(args.setting) if args.setting == int(args.setting) else args.setting

    ts = args.ts or datetime.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    log: list[dict] = []
    if GRIND_LOG.is_file():
        try:
            log = json.loads(GRIND_LOG.read_text()) or []
        except json.JSONDecodeError:
            print(f"grind_log.json is corrupt; refusing to overwrite", file=sys.stderr)
            return 2

    entry: dict = {
        "ts_utc": ts,
        "setting": setting,
    }
    if args.note:
        entry["note"] = args.note
    log.append(entry)

    GRIND_LOG.parent.mkdir(parents=True, exist_ok=True)
    tmp = GRIND_LOG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(log, indent=2))
    os.replace(tmp, GRIND_LOG)
    print(f"logged: grinder setting {setting} (total entries: {len(log)})")

    if not args.no_backfill:
        subprocess.run([sys.executable, str(BACKFILL), "--quiet"], check=False)
        print("backfill complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
