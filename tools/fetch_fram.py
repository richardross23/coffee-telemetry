#!/usr/bin/env python3
"""Fetch the Mahlkönig E65S GBW grinder's FRAM dump.

The on-device shot log lives in non-volatile FRAM on the grinder MCU,
NOT in the rotating RAM log that /logs? reads. To extract it:

    POST /stm?framFetch       # tells the WiFi module to slurp FRAM into a file
    GET  /status              # poll until status.framProgress == 100
    GET  /fram.bin            # download the binary blob

Run this from a host on the grinder's diagnostic AP (default
http://192.168.4.1, no auth). Pure stdlib, no pip deps.

Usage:
    python3 fetch_fram.py
    python3 fetch_fram.py --base http://192.168.4.1 --out fram.bin
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from urllib.error import URLError

DEFAULT_BASE = "http://192.168.4.1"
POLL_INTERVAL_SEC = 1.5  # WiFi module is memory-tight, don't hammer it
POLL_TIMEOUT_SEC = 120   # full FRAM dump shouldn't take longer than this
HTTP_TIMEOUT_SEC = 6.0


def request(base: str, path: str, *, method: str = "GET") -> tuple[int, bytes]:
    req = urllib.request.Request(base + path, method=method)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as r:
        return r.status, r.read()


def status_progress(base: str) -> int:
    """Read framProgress (0..100) from /status. The /status JSON can
    occasionally truncate at ~1 KB; we're only after one integer field
    so do a permissive parse."""
    code, body = request(base, "/status")
    text = body.decode(errors="replace")
    # Substring match — works even if the JSON is truncated past the field.
    marker = '"framProgress":'
    i = text.find(marker)
    if i < 0:
        return -1
    j = i + len(marker)
    # read digits
    while j < len(text) and text[j] in " \t":
        j += 1
    end = j
    while end < len(text) and text[end].isdigit():
        end += 1
    try:
        return int(text[j:end])
    except ValueError:
        return -1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", default=DEFAULT_BASE, help=f"(default: {DEFAULT_BASE})")
    p.add_argument("--out", default="fram.bin", help="(default: fram.bin in cwd)")
    args = p.parse_args()

    print(f"trigger FRAM fetch at {args.base}")
    try:
        code, body = request(args.base, "/stm?framFetch", method="POST")
        print(f"  POST /stm?framFetch → HTTP {code}")
    except URLError as e:
        print(f"failed: {e}", file=sys.stderr)
        print("are you on the grinder's diagnostic AP?", file=sys.stderr)
        return 1

    # Poll /status until framProgress hits 100.
    print("polling /status for framProgress …")
    deadline = time.time() + POLL_TIMEOUT_SEC
    last = -1
    while time.time() < deadline:
        try:
            progress = status_progress(args.base)
        except URLError as e:
            print(f"  status fetch failed (will retry): {e}")
            time.sleep(POLL_INTERVAL_SEC)
            continue
        if progress != last:
            print(f"  framProgress = {progress}%")
            last = progress
        if progress >= 100:
            break
        time.sleep(POLL_INTERVAL_SEC)
    else:
        print(f"timed out after {POLL_TIMEOUT_SEC}s — FRAM dump didn't reach 100%", file=sys.stderr)
        return 2

    # Download the blob.
    print(f"downloading /fram.bin → {args.out}")
    try:
        code, blob = request(args.base, "/fram.bin")
    except URLError as e:
        print(f"download failed: {e}", file=sys.stderr)
        return 3

    with open(args.out, "wb") as f:
        f.write(blob)
    print(f"  HTTP {code}, {len(blob)} bytes saved")

    # Quick sniff so we know the file isn't empty / an error page.
    print(f"\nfirst 64 bytes (hex):  {blob[:64].hex()}")
    printable = sum(1 for b in blob[:1024] if 32 <= b < 127 or b in (9, 10, 13))
    print(f"printable bytes in first 1KB: {printable}/1024")
    return 0


if __name__ == "__main__":
    sys.exit(main())
