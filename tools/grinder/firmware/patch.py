#!/usr/bin/env python3
"""Patch the Mahlkönig E65S GbW SW300 STM32 image (HEM_NGFW300e.bin).

Patches (choose with --patches, default: all):
  actualdose  End-of-dose screen shows the measured weight (state+0x90)
              instead of the target (state+0x40).
  ddd         Weight-based shot records store the live disc distance
              (state+0xAC, signed, µm) in the spare field at record+0xD,
              which is FRAM rec[17:21]. Read it as int32 LE / 1000 = mm.
  shotlog     Shot Log is made visible without owner login, two bytes:
              (1) Machine Logs list entry (menu 5, id 0x1e) level 1 -> 0
                  at 0x0804B378 — the actual gate that hid it.
              (2) Shot Log detail header (menu 11, id 0x71) gated level
                  -> 0 at 0x08047348, so the detail view's title shows too.
              Error/Service/Alerts log entries are left owner-gated.

Usage: python3 -I patch.py <original HEM_NGFW300e.bin> <output.bin> [--patches actualdose,ddd,shotlog]
"""
import argparse, hashlib, struct, sys

EXPECTED_SHA256 = "bdb66ad30c4db40ab3d0db3034b8693aed878bf47b934370f89a8a6a8ed4882b"
BASE = 0x08000000


def thumb_branch(src, dst, link):
    """Encode a Thumb-2 BL (link=True) or B.W (link=False) from src to dst."""
    off = dst - (src + 4)
    assert -(1 << 24) <= off < (1 << 24) and off % 2 == 0
    s = (off >> 24) & 1
    i1 = (off >> 23) & 1
    i2 = (off >> 22) & 1
    j1 = (~(i1 ^ s)) & 1
    j2 = (~(i2 ^ s)) & 1
    hw1 = 0xF000 | (s << 10) | ((off >> 12) & 0x3FF)
    hw2 = (0xD000 if link else 0x9000) | (j1 << 13) | (j2 << 11) | ((off >> 1) & 0x7FF)
    return struct.pack("<HH", hw1, hw2)


# actualdose: ldr r2,[r3,#0x40]; r2*=10  ->  ldr.w r2,[r3,#0x90]; r2*=10; nop
ACTUALDOSE = [
    (0x0804D392,
     bytes.fromhex("1a6c13469b0013445b001a46"),
     bytes.fromhex("d3f8902002eb8202520000bf")),
]

# ddd: redirect the weight-based shot's log-append call through a small stub
CALL_SITE = 0x0800B1B8   # bl log_append (weight-based shot record)
LOG_APPEND = 0x0801C03C
CAVE = 0x08183760        # unused zero padding after the .data load image
STATE = 0x24058F80
DDD_STUB = (
    struct.pack("<H", 0x4B03)                       # ldr r3,[pc,#12] -> STATE
    + bytes.fromhex("b3f9ac30")                     # ldrsh.w r3,[r3,#0xac]
    + bytes.fromhex("c0f80d30")                     # str.w r3,[r0,#0xd]
    + thumb_branch(CAVE + 0xA, LOG_APPEND, False)   # b.w log_append
    + bytes.fromhex("00bf")                         # nop (literal alignment)
    + struct.pack("<I", STATE)
)
DDD = [
    (CALL_SITE, thumb_branch(CALL_SITE, LOG_APPEND, True), thumb_branch(CALL_SITE, CAVE, True)),
    (CAVE, bytes(len(DDD_STUB)), DDD_STUB),
]

# shotlog: two level immediates lowered to 0 so the Shot Log row shows
# for a non-owner login. 0x0804B378 is the Machine Logs list entry (the
# gate); 0x08047348 is the detail-view header.
SHOTLOG = [
    (0x0804B378,
     bytes.fromhex("0123"),   # movs r3,#1  (list entry id 0x1e, menu 5)
     bytes.fromhex("0023")),  # movs r3,#0
    (0x08047348,
     bytes.fromhex("0523"),   # movs r3,#5  (detail header id 0x71, menu 11)
     bytes.fromhex("0023")),  # movs r3,#0
]

PATCHES = {"actualdose": ACTUALDOSE, "ddd": DDD, "shotlog": SHOTLOG}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--patches", default="actualdose,ddd,shotlog")
    a = ap.parse_args()

    d = bytearray(open(a.src, "rb").read())
    if hashlib.sha256(d).hexdigest() != EXPECTED_SHA256:
        sys.exit("input is not the expected SW300 image")

    for name in a.patches.split(","):
        for addr, old, new in PATCHES[name]:
            off = addr - BASE
            assert len(old) == len(new)
            if d[off:off + len(old)] != old:
                sys.exit(f"{name}: original bytes not found at {addr:#x}")
            d[off:off + len(new)] = new
        print("applied", name)

    open(a.dst, "wb").write(d)
    print("wrote", a.dst, "sha256", hashlib.sha256(d).hexdigest(), "md5", hashlib.md5(d).hexdigest())


if __name__ == "__main__":
    main()
