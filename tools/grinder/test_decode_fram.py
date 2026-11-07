#!/usr/bin/env python3
"""Tests for decode_fram.py. Stdlib only:

    python3 tools/grinder/test_decode_fram.py
"""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from decode_fram import REGION_START, decode


def record(flags: int, ts: int, target_cg: int = 2000, actual_cg: int = 1950,
           ddd_um: int = 0, grind_ms: int = 5500, rest_ms: int = 0) -> bytes:
    return (bytes([flags, 0, 0, 0, 0x08])
            + struct.pack("<IIIiII", ts, target_cg, actual_cg, ddd_um, grind_ms, rest_ms))


def fram(*records: bytes) -> Path:
    buf = bytearray(REGION_START) + b"".join(records)
    buf += bytes(64 * 1024 - len(buf))
    f = tempfile.NamedTemporaryFile(suffix=".bin", delete=False)
    f.write(buf)
    f.close()
    return Path(f.name)


class DecodeTest(unittest.TestCase):
    def test_reads_records_whatever_the_flags_byte(self):
        # rec[0] is a per-record flags byte (0/1/2 seen in real dumps), not a
        # fixed "valid" marker; records with rec[0]=0 must not be skipped.
        path = fram(
            record(1, 1_760_000_000),
            record(0, 1_760_000_600),
            record(2, 1_760_001_200),
        )
        self.assertEqual(len(decode(path)), 3)

    def test_marker_bytes_inside_a_record_are_not_a_record(self):
        # grind 5384 ms = 08 15 00 00, so rec[18:22] reads 00 00 00 08 —
        # the same bytes as a header. That must not decode as a 1970 record.
        path = fram(
            record(0, 1_760_000_000, grind_ms=0x1508),
            record(0, 1_760_000_600),
        )
        self.assertEqual(len(decode(path)), 2)


if __name__ == "__main__":
    unittest.main()
