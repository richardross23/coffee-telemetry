#!/usr/bin/env python3
"""Probe the Mahlkönig E65S GBW grinder over its diagnostic Wi-Fi AP.

Connect the host running this to the grinder's AP (default IP
http://192.168.4.1), then run this script. It hits every documented
endpoint and prints a diagnostic dump:

    /info          → firmware version, model, serial
    /status        → runtime state including framProgress
    /stats         → usage / Wi-Fi statistics
    /logs?Shots    → drains the paginated shot log
    /logs?Errors   → error log
    /logs?Service  → service log

Useful when the rotating log appears stuck — quickly see if it's a
clock issue, firmware version mismatch, or something else.

Usage:
    python3 tools/probe_grinder.py
    python3 tools/probe_grinder.py --base http://192.168.4.1
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from urllib.error import URLError

DEFAULT_BASE = "http://192.168.4.1"


def fetch(base: str, path: str, *, timeout: float = 6.0) -> tuple[int, bytes]:
    req = urllib.request.Request(base + path)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def fetch_json(base: str, path: str):
    code, body = fetch(base, path)
    return code, json.loads(body) if body.strip() else None


def section(title: str) -> None:
    print(f"\n══════ {title} ══════")


def dump_json(label: str, base: str, path: str) -> None:
    section(f"{label}  ({path})")
    try:
        code, data = fetch_json(base, path)
        print(f"HTTP {code}")
        print(json.dumps(data, indent=2, default=str))
    except URLError as e:
        print(f"FAILED: {e}")
    except (json.JSONDecodeError, ValueError) as e:
        print(f"NON-JSON RESPONSE: {e}")


def drain_log(base: str, log_type: str) -> None:
    section(f"{log_type} log  (/logs?{log_type})")
    try:
        # Reset cursor
        fetch(base, "/logs?Reset")
        all_lines: list[str] = []
        chunks = 0
        while True:
            code, chunk = fetch_json(base, f"/logs?{log_type}")
            if not isinstance(chunk, list) or not chunk:
                break
            all_lines.extend(chunk)
            chunks += 1
            if chunks > 200:  # sanity cap
                print("(stopped after 200 chunks)")
                break
        n = len(all_lines)
        print(f"chunks: {chunks}   entries: {n}")
        if n:
            print(f"first:  {all_lines[0]}")
            print(f"last :  {all_lines[-1]}")
    except URLError as e:
        print(f"FAILED: {e}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", default=DEFAULT_BASE, help=f"(default: {DEFAULT_BASE})")
    args = p.parse_args()

    print(f"probing {args.base}\n")

    # Quick reachability check
    try:
        fetch(args.base, "/info", timeout=4.0)
    except URLError as e:
        print(f"can't reach {args.base}: {e}", file=sys.stderr)
        print("Are you connected to the grinder's diagnostic AP?", file=sys.stderr)
        return 1

    dump_json("device info", args.base, "/info")
    dump_json("status", args.base, "/status")
    dump_json("stats", args.base, "/stats")

    drain_log(args.base, "Shots")
    drain_log(args.base, "Errors")
    drain_log(args.base, "Service")

    print("\n--- next steps ---")
    print("If first/last timestamps in the Shots log look stale (e.g. months ago),")
    print("the rotating buffer is stuck. Check the grinder display for the current")
    print("time/date. If the clock is wrong, the firmware is likely silently")
    print("dropping new entries.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
