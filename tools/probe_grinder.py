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
import time
import urllib.request
from urllib.error import URLError

DEFAULT_BASE = "http://192.168.4.1"
# The WiFi module is memory-constrained — its HTTP response buffer
# truncates around 1 KB and rapid paginated requests have caused it to
# crash and reboot. Throttle our log-drain loop to give it air.
PAGE_DELAY_SEC = 0.4
PAGE_TIMEOUT_SEC = 4.0


def fetch(base: str, path: str, *, timeout: float = PAGE_TIMEOUT_SEC) -> tuple[int, bytes]:
    req = urllib.request.Request(base + path)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def fetch_json(base: str, path: str):
    code, body = fetch(base, path)
    if not body.strip():
        return code, None
    try:
        return code, json.loads(body)
    except (json.JSONDecodeError, ValueError) as e:
        # The WiFi firmware truncates large responses around 1 KB. Return
        # the raw body so the caller can decide what to do.
        return code, {"__truncated__": True, "raw": body.decode(errors="replace"), "error": str(e)}


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


def drain_log(base: str, log_type: str, *, full: bool = False) -> None:
    section(f"{log_type} log  (/logs?{log_type})")
    try:
        fetch(base, "/logs?Reset")
        time.sleep(PAGE_DELAY_SEC)
        all_lines: list[str] = []
        chunks = 0
        while True:
            try:
                code, chunk = fetch_json(base, f"/logs?{log_type}")
            except URLError as e:
                print(f"chunk {chunks} request failed: {e}")
                break
            if not isinstance(chunk, list) or not chunk:
                break
            all_lines.extend(chunk)
            chunks += 1
            if chunks > 500:
                print("(stopped after 500 chunks)")
                break
            time.sleep(PAGE_DELAY_SEC)
        n = len(all_lines)
        print(f"chunks: {chunks}   entries: {n}")
        if n:
            print(f"first:  {all_lines[0]}")
            print(f"last :  {all_lines[-1]}")
            if full:
                print()
                for line in all_lines:
                    print(line)
    except URLError as e:
        print(f"FAILED: {e}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", default=DEFAULT_BASE, help=f"(default: {DEFAULT_BASE})")
    p.add_argument(
        "--log-type",
        choices=["Shots", "Errors", "Service", "Info", "all", "none"],
        default="none",
        help="Which log buffer to drain. Default: none (just /info, /status, /stats). "
             "Drain logs separately to avoid stressing the WiFi module's RAM.",
    )
    p.add_argument("--full", action="store_true", help="Print every log line in the drained buffer (not just first/last).")
    args = p.parse_args()

    print(f"probing {args.base}")
    print(f"page delay {PAGE_DELAY_SEC}s, timeout {PAGE_TIMEOUT_SEC}s\n")

    try:
        fetch(args.base, "/info")
    except URLError as e:
        print(f"can't reach {args.base}: {e}", file=sys.stderr)
        print("Are you connected to the grinder's diagnostic AP?", file=sys.stderr)
        return 1

    dump_json("device info", args.base, "/info")
    dump_json("status", args.base, "/status")
    dump_json("stats", args.base, "/stats")

    if args.log_type == "all":
        for t in ("Shots", "Errors", "Service"):
            drain_log(args.base, t, full=args.full)
            time.sleep(1.0)
    elif args.log_type != "none":
        drain_log(args.base, args.log_type, full=args.full)

    print("\n--- notes ---")
    print("If first/last timestamps in the Shots log look stale, the rotating")
    print("buffer is stuck. Check the grinder display for the current date.")
    print("If the clock is wrong, the firmware is likely dropping new entries.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
