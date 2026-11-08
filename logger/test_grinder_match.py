#!/usr/bin/env python3
"""Tests for attaching a reported grind to the shot it was made for. Stdlib only:

    python3 logger/test_grinder_match.py
"""

from __future__ import annotations

import glob
import json
import os
import sys
import tempfile
import time
import types
import unittest

# logger.py imports paho, which only the container has.
for name in ("paho", "paho.mqtt", "paho.mqtt.client"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["paho"].mqtt = sys.modules["paho.mqtt"]
sys.modules["paho.mqtt"].client = sys.modules["paho.mqtt.client"]

import logger  # noqa: E402

TS = 1_794_210_235
SAMPLES = [{"t_ms": 0, "weight_g": 0.0}, {"t_ms": 30_000, "weight_g": 39.0}]


def grind(ts=TS, actual=2001, seen_ago=90.0, disc=156):
    return {"ts": ts, "target_cg": 2000, "actual_cg": actual, "disc_um": disc,
            "grind_ms": 5395, "flag": 0, "seen_at": time.time() - seen_ago}


class GrindMatchTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        logger.OUT = tmp.name
        logger._index.clear()
        logger.shots_in_flight.clear()
        logger.recent_grinds.clear()

    def finish(self, shot_id):
        logger.handle_shot_end(shot_id, {"duration_s": 30, "final_weight_g": 39.0,
                                         "samples": list(SAMPLES)})
        path, = glob.glob(os.path.join(logger.OUT, f"*_{shot_id}.json"))
        with open(path) as f:
            return json.load(f).get("metadata") or {}

    def shot(self, shot_id):
        logger.handle_shot_start(shot_id, {})
        return self.finish(shot_id)

    def test_grind_two_minutes_before_the_shot_is_attached(self):
        logger.handle_grinder_last(grind(seen_ago=120))
        meta = self.shot("a")
        self.assertEqual(meta["ground_weight_g"], 20.01)
        self.assertEqual(meta["grind_duration_s"], 5.395)
        self.assertEqual(meta["grinder_setting"], 156)
        self.assertEqual(meta["grinder_raw_ts"], TS)
        self.assertEqual(meta["grinder_source"], "live")
        self.assertAlmostEqual(meta["grinder_match_gap_s"], 120, delta=2)

    def test_newest_grind_before_the_shot_wins(self):
        logger.handle_grinder_last(grind(ts=TS - 200, actual=1900, seen_ago=300))
        logger.handle_grinder_last(grind(seen_ago=90))
        self.assertEqual(self.shot("a")["grinder_raw_ts"], TS)

    def test_grind_during_a_shot_is_kept_for_the_next_shot(self):
        logger.handle_shot_start("a", {})
        time.sleep(0.01)
        logger.handle_grinder_last(grind(seen_ago=0))
        self.assertNotIn("ground_weight_g", self.finish("a"))
        time.sleep(0.01)
        self.assertEqual(self.shot("b")["grinder_raw_ts"], TS)

    def test_grind_older_than_the_lookback_is_ignored(self):
        logger.handle_grinder_last(grind(seen_ago=logger.GRIND_LOOKBACK_SEC + 5))
        self.assertNotIn("ground_weight_g", self.shot("a"))

    def test_each_grind_is_used_once(self):
        logger.handle_grinder_last(grind())
        self.assertIn("ground_weight_g", self.shot("a"))
        self.assertNotIn("ground_weight_g", self.shot("b"))

    def test_aborted_shot_leaves_the_grind_for_the_real_one(self):
        logger.handle_grinder_last(grind())
        logger.handle_shot_start("a", {})
        logger.handle_shot_end("a", {"aborted": True, "reason": "no_flow"})
        self.assertIn("ground_weight_g", self.shot("b"))

    def test_junk_grind_is_ignored(self):
        logger.handle_grinder_last(grind(actual=0))
        self.assertNotIn("ground_weight_g", self.shot("a"))

    def test_redelivered_grind_is_kept_once(self):
        g = grind()
        logger.handle_grinder_last(g)
        logger.handle_grinder_last(dict(g))
        self.assertEqual(len(logger.recent_grinds), 1)

    def set_sticky_setting(self, shot_id, value):
        path, = glob.glob(os.path.join(logger.OUT, f"*_{shot_id}.json"))
        name = os.path.basename(path)
        logger.apply_metadata(name, {"grinder_setting": value})
        logger.update_index(name)

    def test_measured_disc_distance_replaces_the_inherited_setting(self):
        self.shot("a")
        self.set_sticky_setting("a", 150)
        logger.handle_grinder_last(grind())
        self.assertEqual(self.shot("b")["grinder_setting"], 156)

    def test_unrecorded_disc_distance_keeps_the_inherited_setting(self):
        self.shot("a")
        self.set_sticky_setting("a", 150)
        logger.handle_grinder_last(grind(disc=0))
        self.assertEqual(self.shot("b")["grinder_setting"], 150)


if __name__ == "__main__":
    unittest.main()
