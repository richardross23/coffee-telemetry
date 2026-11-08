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
class DefaultBuildTest(unittest.TestCase):
    def test_default_build_is_the_published_image(self):
        image = build("actualdose", "ddd", "shotlog")
        self.assertEqual(hashlib.md5(image).hexdigest(), "8c95871295a657f04c389b39e08dea8e")


if __name__ == "__main__":
    unittest.main()
