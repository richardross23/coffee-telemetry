#!/usr/bin/env python3
"""Emulator tests for patch.py. Needs `pip install unicorn` and the stock image
as HEM_NGFW300e.original.bin in this folder (it is not in the repo):

    python3 tools/grinder/firmware/test_patches.py
"""

from __future__ import annotations

import calendar
import hashlib
import json
import random
import re
import struct
import time
import unittest
from pathlib import Path

import patch

STOCK = Path(__file__).with_name("HEM_NGFW300e.original.bin")
if STOCK.exists():
    from unicorn import UC_HOOK_CODE
    from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R7

    from emu_grinder import (LAST_SHOT, STATE, SUB_STATS, WIFI_FLAG, Grinder)

SIZE = 0x8000
FIRST_TS = 1_700_000_000
WEIGHT_ROW = re.compile(r"\w+, (\S+ \S+), Recipe +\d, ground, ([\d.]+)g, ([\d.]+)g, ([\d.]+)s, (-?[\d.]+)m$")


def build(*names):
    image = bytearray(STOCK.read_bytes())
    for name in names:
        for addr, old, new in patch.PATCHES[name]:
            off = addr - patch.BASE
            assert image[off:off + len(old)] == old, f"{name} at {addr:#x}"
            image[off:off + len(new)] = new
    return image


def shot(n):
    return bytes([8]) + struct.pack("<IIIiIII", FIRST_TS + n * 60, 2000, 1975 + n % 50,
                                    n % 200 - 20, 5500 + n % 300, 0, 0)


def fram_with_shots(head, count):
    """A FRAM image whose shot ring holds `count` records ending at `head`."""
    fram = bytearray(0x40000)
    first = head - 29 * count
    for n in range(count):
        for j, b in enumerate(shot(n)):
            fram[0x1080 + (first + 29 * n + j) % SIZE] = b
    tail = (head + SIZE - 29 * count) % SIZE
    fram[0x1000:0x100C] = struct.pack("<III", tail, head, count)
    return bytes(fram)


def timestamps(rows):
    return [calendar.timegm(time.strptime(r.split(", ")[1], "%Y/%m/%d %H:%M:%S")) for r in rows]


@unittest.skipUnless(STOCK.exists(), "stock image not present")
class LogsPatchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stock = build()
        cls.patched = build("ddd", "logs")

    def test_stock_reader_overruns_at_the_end_of_the_ring(self):
        # Tail just before the end of the ring, so a drain crosses it at once.
        g = Grinder(self.stock, fram_with_shots(0x2000 + 27, 1129))
        g.drain()
        self.assertTrue(g.overruns)

    def test_first_page_is_the_newest_shots(self):
        g = Grinder(self.patched, fram_with_shots(0x4000, 200))
        g.request(0, reset=1)
        rows = json.loads(g.respond(0x0C))
        want = [FIRST_TS + n * 60 for n in range(199, 199 - len(rows), -1)]
        self.assertEqual(timestamps(rows), want)

    def test_drain_is_complete_wherever_the_ring_wraps(self):
        random.seed(1)
        cases = [(0x7FFF, 300), (1, 300), (4, 300), (28, 300), (29, 300), (0x7FE7, 300),
                 (29 * 5, 5), (29 * 5, 1), (0x2000, 1129)]
        cases += [(random.randrange(SIZE), random.randrange(1, 400)) for _ in range(6)]
        for head, count in cases:
            with self.subTest(head=hex(head), count=count):
                g = Grinder(self.patched, fram_with_shots(head, count))
                rows = g.drain()
                self.assertIsNone(g.fault)
                self.assertEqual(g.overruns, [])
                self.assertEqual(timestamps(rows),
                                 [FIRST_TS + n * 60 for n in range(count - 1, -1, -1)])

    def test_rows_carry_two_decimals_and_disc_distance(self):
        g = Grinder(self.patched, fram_with_shots(0x1000, 3))
        m = WEIGHT_ROW.match(g.drain()[2])            # oldest row is shot 0
        self.assertEqual(m.group(2, 3, 4, 5), ("20.00", "19.75", "5.500", "-0.020"))

    def test_other_logs_still_read_oldest_first(self):
        fram = bytearray(0x40000)
        size, base, count = 0x4000, 0x11080, 40
        tail = size - 100                             # wraps partway through
        for n in range(count):
            for j, b in enumerate(bytes([7]) + struct.pack("<I", FIRST_TS + n)):
                fram[base + (tail + 5 * n + j) % size] = b
        fram[0x1000 + 24:0x100C + 24] = struct.pack("<III", tail, (tail + 5 * count) % size, count)
        g = Grinder(self.patched, bytes(fram))
        self.assertEqual(timestamps(g.drain(logtype=2)), [FIRST_TS + n for n in range(count)])
        self.assertEqual(g.overruns, [])

    def test_empty_log(self):
        g = Grinder(self.patched, bytes(0x40000))
        g.request(0, reset=1)
        self.assertEqual(g.respond(0x0C), "[]")


@unittest.skipUnless(STOCK.exists(), "stock image not present")
class LastStatPatchTest(unittest.TestCase):
    def test_stats_carries_the_last_shot(self):
        g = Grinder(build("ddd", "laststat"), bytes(0x40000))
        record = bytes([8]) + struct.pack("<IIIiI", 1794130826, 2000, 2011, -156, 5537)
        g.mu.mem_write(LAST_SHOT, record + bytes(4) + bytes([0x12]))
        fields = dict(re.findall(r'"(last\w+)":"(-?\d+)"', g.respond(SUB_STATS)))
        self.assertEqual(fields, {"lastTs": "1794130826", "lastTg": "2000", "lastAc": "2011",
                                  "lastDisc": "-156", "lastGrnd": "5537", "lastFlag": "18"})


@unittest.skipUnless(STOCK.exists(), "stock image not present")
class WifiBootPatchTest(unittest.TestCase):
    KEYS, DONE = 0x08008A6C, 0x0800B9A4

    def boot(self, image, keys_held):
        g = Grinder(image, bytes(0x40000))
        g.replace(self.KEYS, lambda: keys_held)
        g.call(0x0800A2D4, stop=self.DONE, count=100)
        self.assertIsNone(g.fault)
        return (g.mu.mem_read(WIFI_FLAG, 1)[0], g.mu.mem_read(STATE + 0xF5, 1)[0])

    def test_wifi_needs_both_keys_on_stock(self):
        self.assertEqual(self.boot(build(), 0), (0, 0))

    def test_wifi_starts_without_owner_login(self):
        self.assertEqual(self.boot(build("wifiboot"), 0), (1, 0))

    def test_both_keys_still_log_in_as_owner(self):
        self.assertEqual(self.boot(build("wifiboot"), 1), (1, 1))


@unittest.skipUnless(STOCK.exists(), "stock image not present")
class EndDecimalsPatchTest(unittest.TestCase):
    SCREEN, DIGITS_USED, THIS = 0x0804C84C, 0x0804D5DA, 0x24070000
    SETTINGS, RECIPES, DISPLAY = 0x240593A0, 0x24059558, 0x24055C6C
    TIME_DIGITS, WEIGHT_DIGITS = 3, 1
    END_OF_SHOT, GRINDING, TIME_RESULT = 0x101, 0x82, 0x84

    def screen(self, image, state, weight_recipe=True, recorded=0):
        """Run the dose screen's update for a state. Returns (decimals, displayed)."""
        g = Grinder(image, bytes(0x40000))
        g.stub_calls(self.SCREEN, 0x0804DA00)
        mu = g.mu
        mu.mem_write(STATE, struct.pack("<H", state))
        mu.mem_write(STATE + 0x40, struct.pack("<I", 2000))
        mu.mem_write(STATE + 0x90, struct.pack("<I", 2011))
        mu.mem_write(STATE + 0x88, bytes([1]))
        mu.mem_write(LAST_SHOT + 9, struct.pack("<I", recorded))
        mu.mem_write(self.SETTINGS + 0x74, bytes([self.TIME_DIGITS]))
        mu.mem_write(self.SETTINGS + 0xED, bytes([self.WEIGHT_DIGITS]))
        mu.mem_write(self.RECIPES, bytes([0x80 if weight_recipe else 0]))
        seen = {}

        def at_digits(mu, _addr, _size, _user):
            r7 = mu.reg_read(UC_ARM_REG_R7)
            seen["digits"] = struct.unpack("<I", bytes(mu.mem_read(r7 + 0x50, 4)))[0]
            seen["display"] = struct.unpack("<I", bytes(mu.mem_read(self.DISPLAY, 4)))[0]

        mu.hook_add(UC_HOOK_CODE, at_digits, begin=self.DIGITS_USED, end=self.DIGITS_USED)
        mu.reg_write(UC_ARM_REG_R0, self.THIS)
        g.call(self.SCREEN, count=400_000)
        self.assertIsNone(g.fault)
        return seen["digits"], seen["display"]

    @classmethod
    def setUpClass(cls):
        cls.stock = build()
        cls.patched = build("actualdose", "enddecimals")
        cls.final = build("actualdose", "enddecimals", "finalweight")

    def test_end_of_shot_shows_the_measured_weight_to_two_decimals(self):
        self.assertEqual(self.screen(self.patched, self.END_OF_SHOT), (2, 20110))

    def test_end_of_shot_shows_the_weight_in_the_shot_record(self):
        self.assertEqual(self.screen(self.final, self.END_OF_SHOT, recorded=2036), (2, 20360))

    def test_end_of_shot_is_live_until_the_first_steady_reading(self):
        self.assertEqual(self.screen(self.final, self.END_OF_SHOT, recorded=0), (2, 20110))

    def test_stock_end_of_shot_follows_the_setting(self):
        self.assertEqual(self.screen(self.stock, self.END_OF_SHOT), (self.WEIGHT_DIGITS, 20000))

    def test_other_screens_keep_their_decimals(self):
        for state in (self.GRINDING, self.TIME_RESULT, 0x102, 0x103):
            with self.subTest(state=hex(state)):
                self.assertEqual(self.screen(self.patched, state)[0],
                                 self.screen(self.stock, state)[0])

    def test_time_based_recipe_keeps_its_decimals(self):
        self.assertEqual(self.screen(self.patched, self.END_OF_SHOT, weight_recipe=False)[0],
                         self.screen(self.stock, self.END_OF_SHOT, weight_recipe=False)[0])


@unittest.skipUnless(STOCK.exists(), "stock image not present")
class FinalWeightPatchTest(unittest.TestCase):
    STEADY_CHECK, DONE, FRAME = 0x0800AFC6, 0x0800AFE0, 0x24070000
    LIVE, RECORDED, LIMIT, STEADY, UNSTEADY = 2011, 2036, 50, 10, 80
    MACHINE, MACHINE_END, LOG_APPEND = 0x08009CE4, 0x0800BC40, 0x0801C03C
    SETTINGS, END_OF_SHOT = 0x240593A0, 0x101

    @classmethod
    def setUpClass(cls):
        cls.stock = build()
        cls.patched = build("actualdose", "finalweight")

    def after_a_pass(self, image, ms, recorded, noise=STEADY):
        """The shot record's weight after one pass of the end-of-shot state, ms
        after the motor stopped."""
        g = Grinder(image, bytes(0x40000))
        mu = g.mu
        mu.mem_write(STATE + 0x54, struct.pack("<I", ms))
        mu.mem_write(STATE + 0x90, struct.pack("<I", self.LIVE))
        mu.mem_write(STATE + 0xDC, struct.pack("<I", noise))
        mu.mem_write(LAST_SHOT + 9, struct.pack("<I", recorded))
        mu.mem_write(self.FRAME + 0x3C, struct.pack("<I", self.LIMIT))
        mu.reg_write(UC_ARM_REG_R7, self.FRAME)
        g.call(self.STEADY_CHECK, stop=self.DONE, count=100)
        self.assertIsNone(g.fault)
        return struct.unpack("<I", bytes(mu.mem_read(LAST_SHOT + 9, 4)))[0]

    def test_steady_readings_update_the_record_while_it_settles(self):
        self.assertEqual(self.after_a_pass(self.patched, 1999, self.RECORDED), self.LIVE)

    def test_the_record_locks_two_seconds_after_the_motor_stops(self):
        self.assertEqual(self.after_a_pass(self.patched, 2000, self.RECORDED), self.RECORDED)

    def test_unsteady_readings_never_reach_the_record(self):
        self.assertEqual(self.after_a_pass(self.patched, 500, self.RECORDED, self.UNSTEADY),
                         self.RECORDED)

    def test_a_late_first_steady_reading_is_still_recorded(self):
        self.assertEqual(self.after_a_pass(self.patched, 3000, 0), self.LIVE)

    def test_stock_keeps_rewriting_the_record(self):
        self.assertEqual(self.after_a_pass(self.stock, 60_000, self.RECORDED), self.LIVE)

    def logged_after(self, image, passes):
        """Run the real state machine through (ms, live weight) passes of the end
        of a shot. Returns the weights handed to the log."""
        g = Grinder(image, bytes(0x40000))
        g.stub_calls(self.MACHINE, self.MACHINE_END)
        mu = g.mu
        logged = []
        g.replace(self.LOG_APPEND, lambda: logged.append(
            struct.unpack("<i", bytes(mu.mem_read(LAST_SHOT + 9, 4)))[0]))
        mu.mem_write(STATE + 2, struct.pack("<H", self.END_OF_SHOT))
        mu.mem_write(STATE + 0x40, struct.pack("<I", 2000))
        mu.mem_write(STATE + 0xDC, struct.pack("<I", self.STEADY))
        mu.mem_write(self.SETTINGS + 0x48, struct.pack("<I", 1000))
        mu.mem_write(self.SETTINGS + 0x8C, struct.pack("<H", self.LIMIT))
        for ms, weight in passes:
            mu.mem_write(STATE, struct.pack("<H", self.END_OF_SHOT))
            mu.mem_write(STATE + 0x54, struct.pack("<I", ms))
            mu.mem_write(STATE + 0x90, struct.pack("<i", weight))
            g.call(self.MACHINE, count=200_000)
            self.assertIsNone(g.fault)
        return logged

    def test_the_log_gets_the_locked_weight_when_the_portafilter_is_lifted(self):
        passes = [(300, 2005), (1500, 2031), (2500, 2044), (9000, 2019), (9100, -50)]
        self.assertEqual(self.logged_after(build("actualdose", "ddd", "finalweight"), passes),
                         [2031])

    def test_without_the_patch_the_log_gets_the_last_steady_reading(self):
        passes = [(300, 2005), (1500, 2031), (2500, 2044), (9000, 2019), (9100, -50)]
        self.assertEqual(self.logged_after(build("actualdose", "ddd"), passes), [2019])


@unittest.skipUnless(STOCK.exists(), "stock image not present")
class DefaultBuildTest(unittest.TestCase):
    def test_default_build_is_the_published_image(self):
        image = build("actualdose", "ddd", "shotlog")
        self.assertEqual(hashlib.md5(image).hexdigest(), "8c95871295a657f04c389b39e08dea8e")


if __name__ == "__main__":
    unittest.main()
