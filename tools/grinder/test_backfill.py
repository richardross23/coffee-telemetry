#!/usr/bin/env python3
"""Tests for backfill.py's FRAM parsing and metadata writes. Stdlib only:

    python3 tools/grinder/test_backfill.py
"""

from __future__ import annotations

import unittest
from pathlib import Path

from backfill import apply_backfill, parse_fram
from test_decode_fram import fram, record

TS = 1_794_125_321  # 2026-11-08 08:08:41 grinder wall clock


def shot(grinder_setting=150) -> dict:
    return {"file": "x.json", "path": Path("/nonexistent/x.json"),
            "data": {"metadata": {"grinder_setting": grinder_setting}}}


def grind(ddd_um) -> dict:
    return {"actual_g": 20.26, "grind_s": 5.691, "raw_ts": TS, "ddd_um": ddd_um}


class ParseFramTest(unittest.TestCase):
    def test_reads_disc_distance_from_patched_record(self):
        # Patched firmware stores disc distance (µm) in rec[17:21].
        [g] = parse_fram(fram(record(0, TS, ddd_um=156)))
        self.assertEqual(g["ddd_um"], 156)

    def test_zero_disc_distance_means_not_recorded(self):
        [g] = parse_fram(fram(record(0, TS, ddd_um=0)))
        self.assertIsNone(g["ddd_um"])


class ApplyBackfillTest(unittest.TestCase):
    def test_disc_distance_overrides_typed_grinder_setting(self):
        s = shot(grinder_setting=150)
        apply_backfill([(s, grind(156), -60)], dry_run=True)
        self.assertEqual(s["data"]["metadata"]["grinder_setting"], 156)

    def test_typed_grinder_setting_kept_when_not_recorded(self):
        s = shot(grinder_setting=150)
        apply_backfill([(s, grind(None), -60)], dry_run=True)
        self.assertEqual(s["data"]["metadata"]["grinder_setting"], 150)


if __name__ == "__main__":
    unittest.main()
