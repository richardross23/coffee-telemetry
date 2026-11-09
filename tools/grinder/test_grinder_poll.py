#!/usr/bin/env python3
"""Tests for grinder_poll.py. Stdlib only:

    python3 tools/grinder/test_grinder_poll.py
"""

from __future__ import annotations

import datetime
import time
import unittest

from grinder_poll import GAP_POLLS, Watcher, build_report, dose_drift, parse_stats, shots_in_window

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


class DoseDriftTest(unittest.TestCase):
    def index(self, errors, dose=20.0):
        """Shots in time order, each landing `error` grams off the dose."""
        return [{"saved_at": f"2026-11-01T08:{i:02d}:00Z",
                 "metadata": {"bean_weight_g": dose, "ground_weight_g": round(dose + e, 2),
                              "grinder_raw_ts": 1000 + i}}
                for i, e in enumerate(errors)]

    def test_a_steady_low_run_is_reported(self):
        self.assertEqual(dose_drift(self.index([-0.6] * 10)), -0.6)

    def test_a_steady_high_run_is_reported(self):
        self.assertEqual(dose_drift(self.index([0.45] * 10)), 0.45)

    def test_normal_scatter_is_silent(self):
        self.assertIsNone(dose_drift(self.index([-0.2, 0.2] * 5)))

    def test_a_small_steady_offset_is_silent(self):
        self.assertIsNone(dose_drift(self.index([-0.2] * 10)))

    def test_a_large_median_needs_most_shots_on_one_side(self):
        self.assertIsNone(dose_drift(self.index([-0.6] * 6 + [0.5] * 4)))

    def test_fewer_than_ten_shots_is_silent(self):
        self.assertIsNone(dose_drift(self.index([-0.6] * 9)))

    def test_only_the_newest_ten_count(self):
        self.assertIsNone(dose_drift(self.index([-0.6] * 10 + [0.0] * 10)))
        self.assertEqual(dose_drift(self.index([0.0] * 10 + [-0.6] * 10)), -0.6)

    def test_order_comes_from_the_save_time(self):
        index = self.index([0.0] * 10 + [-0.6] * 10)
        self.assertEqual(dose_drift(list(reversed(index))), -0.6)

    def test_weights_not_from_the_grinder_are_ignored(self):
        # A shot without grinder_raw_ts may carry a weight inherited from the last one.
        index = self.index([-0.6] * 10)
        for entry in index:
            del entry["metadata"]["grinder_raw_ts"]
        self.assertIsNone(dose_drift(index))

    def test_a_changed_dose_is_not_drift(self):
        # Grinding 20 g with the shot records still saying 18 g.
        self.assertIsNone(dose_drift(self.index([2.0] * 10, dose=18.0)))


class BuildReportTest(unittest.TestCase):
    def message(self, shots, outages=(), sync="not needed", filled=0, drift=None):
        return build_report("2026-11-09", shots, 3, list(outages), sync, filled, drift)["message"]

    def test_low_doses_are_mentioned(self):
        self.assertEqual(self.message([shot("08:00")], drift=-0.61),
                         "1 shot, with grinder data. Doses running 0.61 g under target (last 10 shots).")

    def test_high_doses_are_mentioned(self):
        self.assertEqual(self.message([shot("08:00")], drift=0.4),
                         "1 shot, with grinder data. Doses running 0.40 g over target (last 10 shots).")

    def test_no_shots_today_stays_silent_even_with_drift(self):
        self.assertEqual(self.message([], drift=-0.61), "")

    def test_report_carries_the_drift_for_home_assistant(self):
        got = build_report("2026-11-09", [shot("08:00")], 1, [], "not needed", 0, -0.61)
        self.assertEqual(got["dose_drift_g"], -0.61)

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
