#!/usr/bin/env python3
"""Tests for backfill.py's FRAM parsing and metadata writes. Stdlib only:

    python3 tools/grinder/test_backfill.py
"""

from __future__ import annotations

import contextlib
import datetime
import io
import unittest
from pathlib import Path
from unittest import mock

from backfill import apply_backfill, main, match, parse_fram
from test_decode_fram import fram, record

TS = 1_794_125_321  # 2026-11-08 08:08:41 grinder wall clock
UTC = datetime.timezone.utc


def local(day: int, hhmm: str) -> datetime.datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime.datetime(2026, 11, day, h, m)


def shot_at(day: int, hhmm: str) -> dict:
    """Shot whose pump turned on at this Melbourne (AEDT, UTC+11) time."""
    t = local(day, hhmm) - datetime.timedelta(hours=11)
    return {"file": f"{day}@{hhmm}", "pump_on_utc": t.replace(tzinfo=UTC),
            "data": {"metadata": {}}}


def grind_at(day: int, hhmm: str) -> dict:
    """Grind whose grinder clock read this; parse_fram reads it as AEST."""
    t = local(day, hhmm) - datetime.timedelta(hours=10)
    return {"utc": t.replace(tzinfo=UTC), "label": f"{day}@{hhmm}",
            "actual_g": 20.0, "grind_s": 5.5, "raw_ts": 0, "ddd_um": None}


def pairs(matches) -> set[tuple[str, str]]:
    return {(s["file"], g["label"]) for s, g, _ in matches}


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

    def test_unmatched_shot_loses_backfilled_values(self):
        s = shot()
        s["data"]["metadata"].update(ground_weight_g=20.28, grind_duration_s=5.5,
                                     grinder_raw_ts=TS, grinder_match_gap_s=-3780,
                                     bean_weight_g=20)
        apply_backfill([], dry_run=True, unmatched=[s])
        self.assertEqual(s["data"]["metadata"],
                         {"grinder_setting": 150, "bean_weight_g": 20})

    def test_unmatched_shot_keeps_typed_dose(self):
        # No grinder_raw_ts: backfill never wrote this dose, so it stays.
        s = shot()
        s["data"]["metadata"]["ground_weight_g"] = 19.5
        apply_backfill([], dry_run=True, unmatched=[s])
        self.assertEqual(s["data"]["metadata"]["ground_weight_g"], 19.5)


class MatchTest(unittest.TestCase):
    def test_skipped_grind_leaves_its_shot_unmatched(self):
        # 2026-11-04: the 08:02 grind logged 0 g (dropped by parse_fram), and
        # the grinder clock is on daylight time. Each shot must keep its own
        # grind instead of taking the next one.
        shots = [shot_at(4, "08:04"), shot_at(4, "08:09"), shot_at(4, "09:00")]
        grinds = [grind_at(4, "08:07"), grind_at(4, "08:58")]
        self.assertEqual(pairs(match(shots, grinds)),
                         {("4@08:09", "4@08:07"), ("4@09:00", "4@08:58")})

    def test_single_shot_day_borrows_offset_from_nearby_day(self):
        # One shot can't establish a day's clock offset on its own; it uses
        # the 4th's. Read naively, the 07:10 grind (an orphan) looks closest.
        shots = [shot_at(4, "08:09"), shot_at(4, "09:00"), shot_at(5, "08:10")]
        grinds = [grind_at(4, "08:07"), grind_at(4, "08:58"),
                  grind_at(5, "07:10"), grind_at(5, "08:08")]
        self.assertIn(("5@08:10", "5@08:08"), pairs(match(shots, grinds)))


class MainTest(unittest.TestCase):
    def run_main(self, path: Path):
        argv = ["backfill.py", "--fram-bin", str(path), "--dry-run"]
        out = io.StringIO()
        with mock.patch("sys.argv", argv), \
             mock.patch("backfill.load_shots", return_value=[]) as load, \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            return main(), load

    def test_partial_download_is_refused_before_touching_shots(self):
        # 2026-11-01: a 12,288-byte file held only the oldest records. Every
        # newer shot would look unmatched and lose its backfilled values.
        path = fram(record(0, TS))
        with open(path, "r+b") as f:
            f.truncate(12288)
        code, load = self.run_main(path)
        self.assertNotEqual(code, 0)
        load.assert_not_called()

    def test_dump_covering_the_shot_log_is_processed(self):
        code, load = self.run_main(fram(record(0, TS)))
        self.assertEqual(code, 0)
        load.assert_called_once()


if __name__ == "__main__":
    unittest.main()
