#!/usr/bin/env python3
"""Decode a Mahlkönig E65S GBW FRAM dump into the same text format as the
grinder's /logs?Shots endpoint, so it can be piped into import_grinder_log.py.

FRAM layout reverse-engineered from a 256 KiB dump (firmware 3.0,
build 2021-09-21). Shot records live in the region starting at 0x01080
and are 29 bytes each, found by the 4-byte marker `00 00 00 08` at rec[1:5]:

    byte   0    : flags (0/1/2 seen; possibly recipe slot + aborted bit —
                  unconfirmed, see GRINDER-FIRMWARE-PATCH.md)
    bytes  1-4  : `00 00 00 08` (rec[4] = record type, 8 = shot)
    bytes  5-8  : uint32 LE — Unix timestamp (interpreted as the user's
                  local wall clock — the firmware doesn't appear to
                  apply a timezone offset, so the numeric value is the
                  on-grinder display time treated as UTC)
    bytes  9-12 : uint32 LE — target weight × 100 (e.g. 2000 = 20.00g)
    bytes 13-16 : uint32 LE — actual ground weight × 100
    bytes 17-20 : stock firmware: 0 for weight-based shots. Patched firmware
                  (2026-11-07): disc distance, int32 LE, µm. Not decoded here.
    bytes 21-24 : uint32 LE — grind duration in milliseconds
    bytes 25-28 : uint32 LE — inter-burr-pause / rest time in milliseconds

Record state notes (matches the user's existing log conventions):
- ground state is implicit — only 'ground' records are written here
- Recipe number isn't stored explicitly in the record; the firmware
  records all entries under the active recipe, which we can't recover
  from FRAM. Default to "Recipe 1" unless overridden.

The buffer is a ring (filled chronologically with wrap-around); the
2026-11-08 dump held 1129 records, 2025-05-30 → 2026-11-08. An earlier
version matched `01 00 00 00 08` and so saw only the ~124 records with
flags=1, which looked like the grinder had stopped writing in 2025.

Usage:
    python3 decode_fram.py fram.bin
    python3 decode_fram.py fram.bin | python3 import_grinder_log.py
"""

from __future__ import annotations

import argparse
import datetime
import struct
import sys
from pathlib import Path

REGION_START = 0x01080
REGION_END = REGION_START + 33064
RECORD_SIZE = 29
# rec[1:5]. rec[0] is a per-record flags byte (0/1/2 seen), not part of the
# marker — matching it as 01 silently dropped every record flagged 0.
MARKER = b"\x00\x00\x00\x08"
# Same window as backfill.py. The marker bytes also occur inside records
# (e.g. a grind time of 0x1508 ms), so a hit only counts if its timestamp is
# plausible.
VALID_TS_LO = 1_577_836_800  # 2020-01-01
VALID_TS_HI = 2_000_000_000  # 2033-05


def decode(path: Path, recipe: int = 1) -> list[str]:
    data = path.read_bytes()
    positions: list[int] = []
    i = REGION_START + 1
    while i < REGION_END - RECORD_SIZE:
        j = data.find(MARKER, i, REGION_END)
        if j < 0:
            break
        positions.append(j - 1)
        i = j + 1

    lines: list[str] = []
    for p in positions:
        rec = data[p:p + RECORD_SIZE]
        if len(rec) < RECORD_SIZE:
            continue
        ts = struct.unpack("<I", rec[5:9])[0]
        if not (VALID_TS_LO < ts < VALID_TS_HI):
            continue
        target = struct.unpack("<I", rec[9:13])[0] / 100.0
        actual = struct.unpack("<I", rec[13:17])[0] / 100.0
        dur = struct.unpack("<I", rec[21:25])[0] / 1000.0
        rest = struct.unpack("<I", rec[25:29])[0] / 1000.0
        dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
        # The grinder's /logs?Shots endpoint emits times as the user sees
        # them on the grinder display. We replicate that by formatting the
        # timestamp as if it were local wall clock (no zone conversion) —
        # this matches what import_grinder_log.py expects.
        ts_str = dt.strftime("%Y/%m/%d %H:%M:%S")
        lines.append(
            f"Info, {ts_str}, Recipe  {recipe}, ground, "
            f"{target:.1f}g, {actual:.1f}g, {dur:.3f}s, {rest:.3f}s"
        )
    # Sort chronologically — FRAM stores in ring-buffer order, not date order
    lines.sort()
    return lines


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("path", type=Path, help="path to fram.bin")
    p.add_argument("--recipe", type=int, default=1, help="recipe number to assign (FRAM doesn't store it; default 1)")
    args = p.parse_args()

    if not args.path.is_file():
        print(f"file not found: {args.path}", file=sys.stderr)
        return 1

    lines = decode(args.path, recipe=args.recipe)
    if not lines:
        print("no records decoded — is this a FRAM dump?", file=sys.stderr)
        return 2

    for line in lines:
        print(line)
    print(f"\n# decoded {len(lines)} records", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
