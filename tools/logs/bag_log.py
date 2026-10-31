#!/usr/bin/env python3
"""Record a new coffee bag + roast date and backfill the shot archive.

Called from the Mac/phone via SSH; typical invocation:

    ssh refuge python3 /volume2/docker/coffee/tools/logs/bag_log.py \
        "Golden Gate" 2026-10-28

Appends one entry to shots/bag_log.json and immediately runs
backfill_logs.py so the dashboard reflects the new bag on next load.
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
BAG_LOG = Path("/volume2/docker/coffee/shots/bag_log.json")
BACKFILL = Path(__file__).with_name("backfill_logs.py")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("bean", help="Bean/roaster name, e.g. 'Golden Gate'")
    p.add_argument("roast_date", help="YYYY-MM-DD")
    p.add_argument("--note", default="",
                   help="Optional free-text note (e.g. 'went coarser')")
    p.add_argument("--ts", default=None,
                   help="Override timestamp (ISO 8601, UTC). Default: now.")
    p.add_argument("--no-backfill", action="store_true",
                   help="Append the entry but skip the backfill run")
    args = p.parse_args()

    # Validate roast date
    try:
        datetime.date.fromisoformat(args.roast_date)
    except ValueError:
        print(f"invalid roast_date (expected YYYY-MM-DD): {args.roast_date!r}",
              file=sys.stderr)
        return 1

    ts = args.ts or datetime.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    log: list[dict] = []
    if BAG_LOG.is_file():
        try:
            log = json.loads(BAG_LOG.read_text()) or []
        except json.JSONDecodeError:
            print(f"bag_log.json is corrupt; refusing to overwrite", file=sys.stderr)
            return 2

    entry = {
        "ts_utc": ts,
        "bean": args.bean,
        "roast_date": args.roast_date,
    }
    if args.note:
        entry["note"] = args.note
    log.append(entry)

    BAG_LOG.parent.mkdir(parents=True, exist_ok=True)
    tmp = BAG_LOG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(log, indent=2))
    os.replace(tmp, BAG_LOG)
    print(f"logged: bag '{args.bean}' roasted {args.roast_date} (total entries: {len(log)})")

    if not args.no_backfill:
        subprocess.run([sys.executable, str(BACKFILL), "--quiet"], check=False)
        print("backfill complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
