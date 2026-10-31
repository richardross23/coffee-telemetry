#!/usr/bin/env python3
"""Update one or more sticky metadata fields on the latest shot so the
logger's inheritance picks the new value up on the next shot.

The shot logger (logger.py's latest_metadata()) seeds every new shot's
metadata from the most recent prior shot. Fields like `roast_date` and
`grinder_setting` carry forward automatically. To "change the sticky
value" we just update that newest shot in-place; the change propagates
to every shot pulled after it.

Historical shots are left untouched. Only the single newest shot file is
modified (plus an index rebuild so the dashboard reflects it).

Typical use:
    python3 set_sticky.py --roast-date 2026-10-28
    python3 set_sticky.py --grinder-setting 122
    python3 set_sticky.py --roast-date 2026-10-28 --grinder-setting 122

Called from the Mac/phone via SSH:
    ssh refuge python3 /volume2/docker/coffee/tools/sticky/set_sticky.py \
        --roast-date 2026-10-28
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SHOTS_DIR = Path("/volume2/docker/coffee/shots")
TS_FN = re.compile(r"^(\d{8}T\d{6}Z)_.+\.json$")


def latest_shot_path() -> Path | None:
    newest = None
    for fn in os.listdir(SHOTS_DIR):
        if not TS_FN.match(fn):
            continue
        p = SHOTS_DIR / fn
        if newest is None or fn > newest.name:
            newest = p
    return newest


def rebuild_index() -> None:
    subprocess.run(["docker", "restart", "shot-logger"], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--roast-date",
                   help="New roast date, YYYY-MM-DD")
    p.add_argument("--grinder-setting",
                   help="New grinder dial setting (number or label)")
    p.add_argument("--no-rebuild-index", action="store_true",
                   help="Skip the shot-logger restart")
    args = p.parse_args()

    updates: dict[str, object] = {}
    if args.roast_date:
        try:
            datetime.date.fromisoformat(args.roast_date)
        except ValueError:
            print(f"invalid --roast-date (expected YYYY-MM-DD): {args.roast_date!r}",
                  file=sys.stderr)
            return 1
        updates["roast_date"] = args.roast_date
    if args.grinder_setting:
        # Preserve ints as ints so '122' doesn't become '122.0'
        try:
            n = float(args.grinder_setting)
            updates["grinder_setting"] = int(n) if n == int(n) else n
        except ValueError:
            updates["grinder_setting"] = args.grinder_setting

    if not updates:
        print("nothing to update (pass --roast-date and/or --grinder-setting)",
              file=sys.stderr)
        return 1

    path = latest_shot_path()
    if path is None:
        print("no timestamped shots found in", SHOTS_DIR, file=sys.stderr)
        return 2

    data = json.loads(path.read_text())
    meta = data.setdefault("metadata", {})

    changed = []
    for k, v in updates.items():
        if meta.get(k) != v:
            meta[k] = v
            changed.append(f"{k}={v}")

    if not changed:
        print(f"latest shot {path.name} already has those values; no change")
        return 0

    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)
    print(f"updated {path.name}: {', '.join(changed)}")
    print("next shot will inherit these values.")

    if not args.no_rebuild_index:
        rebuild_index()
        print("dashboard index rebuilt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
