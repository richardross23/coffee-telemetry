#!/usr/bin/env python3
"""Tests for grinder_poll.py. Stdlib only:

    python3 tools/grinder/test_grinder_poll.py
"""

from __future__ import annotations

import datetime
import time
import unittest

from grinder_poll import GAP_POLLS, Watcher, build_report, parse_stats, shots_in_window

STATS = ('{"restarts":"1003","shots":"3192","lastTs":"1794210235","lastTg":"2000",'
         '"lastAc":"2001","r1Weight":"5439270","lastDisc":"156","lastGrnd":"5395",'
         '"lastFlag":"0","swVersion":"300","')  # /stats is cut off at ~1 KB


def at(hhmm: str) -> float:
    return time.mktime(time.strptime(f"2026-11-09 {hhmm}", "%Y-%m-%d %H:%M"))


def fields(ts: int) -> dict:
    return {"lastTs": ts, "lastTg": 2000, "lastAc": 2001, "lastDisc": 156,
            "lastGrnd": 5395, "lastFlag": 0}


def shot(hhmm: str, has_grinder: bool = True) -> dict:
    return {"file": f"{hhmm}.json", "start": at(hhmm), "has_grinder": has_grinder}


class ParseStatsTest(unittest.TestCase):
    def test_reads_the_last_shot_from_truncated_json(self):
        got = parse_stats(STATS)
        self.assertEqual(got["lastTs"], 1794210235)
        self.assertEqual(got["lastAc"], 2001)
        self.assertEqual(got["lastDisc"], 156)

    def test_firmware_without_the_patch_gives_nothing(self):
        self.assertIsNone(parse_stats('{"restarts":"1003","r4Time":"0",'))


class WatcherTest(unittest.TestCase):
    def test_first_reading_is_a_baseline(self):
        # After an overnight stay on, /stats still shows yesterday's last shot.
        self.assertIsNone(Watcher().step(fields(100), 0.0))

    def test_new_grind_is_reported_with_the_time_it_was_seen(self):
        w = Watcher()
        w.step(fields(100), 0.0)
        self.assertEqual(w.step(fields(200), 10.0),
                         {"ts": 200, "target_cg": 2000, "actual_cg": 2001, "disc_um": 156,
                          "grind_ms": 5395, "flag": 0, "seen_at": 10.0})

    def test_same_grind_is_reported_once(self):
        w = Watcher()
        w.step(fields(100), 0.0)
        w.step(fields(200), 10.0)
        self.assertIsNone(w.step(fields(200), 20.0))

    def test_zero_while_grinding_is_not_a_grind(self):
        w = Watcher()
        w.step(fields(100), 0.0)
        self.assertIsNone(w.step(fields(0), 10.0))
        self.assertEqual(w.step(fields(200), 20.0)["ts"], 200)

    def test_first_grind_after_power_up_is_reported(self):
        w = Watcher()
        w.step(fields(0), 0.0)
        self.assertEqual(w.step(fields(200), 10.0)["ts"], 200)

    def test_brief_miss_does_not_lose_a_grind(self):
        w = Watcher()
        w.step(fields(100), 0.0)
        w.step(None, 10.0)
        self.assertEqual(w.step(fields(200), 20.0)["ts"], 200)
        self.assertEqual(w.outages, [])

    def test_grind_first_seen_after_an_outage_is_not_reported(self):
        # Its real time is unknown, so it can't be placed against a shot.
        w = Watcher()
        w.step(fields(100), 0.0)
        for i in range(GAP_POLLS):
            w.step(None, 10.0 + i * 10)
        self.assertIsNone(w.step(fields(200), 100.0))
        self.assertEqual(w.outages, [(10.0, 100.0)])
        self.assertEqual(w.step(fields(300), 110.0)["ts"], 300)

    def test_outage_still_open_at_the_end_is_recorded(self):
        w = Watcher()
        w.step(fields(100), 0.0)
        for i in range(GAP_POLLS):
            w.step(None, 10.0 + i * 10)
        w.close(500.0)
        self.assertEqual(w.outages, [(10.0, 500.0)])


class ShotsInWindowTest(unittest.TestCase):
    def entry(self, hhmm, duration, meta):
        saved = datetime.datetime.fromtimestamp(at(hhmm) + duration, datetime.timezone.utc)
        return {"file": hhmm, "saved_at": saved.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "duration_s": duration, "metadata": meta}

    def test_keeps_the_window_and_marks_grinder_data(self):
        index = [self.entry("08:00", 30, {"grinder_raw_ts": 5}),
                 self.entry("09:00", 30, {"bean_weight_g": 20}),
                 self.entry("05:00", 30, {"grinder_raw_ts": 4})]
        got = shots_in_window(index, at("06:30"), at("11:05"))
        self.assertEqual([(s["file"], s["has_grinder"]) for s in got],
                         [("08:00", True), ("09:00", False)])
        self.assertEqual(got[0]["start"], at("08:00"))


class BuildReportTest(unittest.TestCase):
    def message(self, shots, outages=(), sync="not needed", filled=0):
        return build_report("2026-11-09", shots, 3, list(outages), sync, filled)["message"]

    def test_no_shots_is_silent(self):
        self.assertEqual(self.message([]), "")

    def test_grinder_off_all_morning_with_no_shots_is_silent(self):
        self.assertEqual(self.message([], [(at("06:30"), at("11:05"))]), "")

    def test_all_shots_matched(self):
        self.assertEqual(self.message([shot("08:00"), shot("09:00"), shot("09:30")]),
                         "3 shots, all with grinder data.")

    def test_single_shot(self):
        self.assertEqual(self.message([shot("08:00")]), "1 shot, with grinder data.")

    def test_fram_sync_filled_the_gap(self):
        self.assertEqual(self.message([shot("08:00"), shot("09:00")], sync="ok", filled=1),
                         "2 shots, all with grinder data. 1 needed the FRAM sync.")

    def test_missing_shot_names_the_outage_it_fell_in(self):
        shots = [shot("08:00"), shot("08:15", has_grinder=False)]
        outage = (at("08:12"), at("08:20"))
        self.assertEqual(self.message(shots, [outage], sync="failed"),
                         "1 of 2 shots missing grinder data. FRAM sync failed. "
                         "Mac lost the grinder 08:12-08:20.")

    def test_outage_away_from_any_shot_is_not_mentioned(self):
        self.assertEqual(self.message([shot("08:00")], [(at("10:00"), at("10:30"))]),
                         "1 shot, with grinder data.")

    def test_report_carries_the_date_for_home_assistant(self):
        got = build_report("2026-11-09", [shot("08:00", False)], 0, [], "failed", 0)
        self.assertEqual((got["date"], got["shots"], got["missing"], got["fram_sync"]),
                         ("2026-11-09", 1, 1, "failed"))


if __name__ == "__main__":
    unittest.main()
