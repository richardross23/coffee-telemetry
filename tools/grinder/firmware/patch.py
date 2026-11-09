#!/usr/bin/env python3
"""Patch the Mahlkönig E65S GbW SW300 STM32 image (HEM_NGFW300e.bin).

Patches (choose with --patches, default: these three):
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

Optional, flashed on one grinder so far, so not in the default set:
  logs       Fixes /logs running off the end of the shot log. Shots are read
              newest first, with 0.01 g weights and disc distance (mm) in the
              last column of weight-based rows.
  laststat    /stats carries the last shot: lastTs, lastTg, lastAc (0.01 g),
              lastDisc (um, needs ddd), lastGrnd (ms), lastFlag. They replace
              the counters of recipes 4-6. All read 0 until the first grind
              after power-up, and lastTs is 0 while a grind runs.
  wifiboot    The Wi-Fi module starts at every power-up, without owner login.
              Holding both keys still logs in as owner. The access point is
              then always on, with the published default password.
  enddecimals The end-of-shot weight shows two decimals. Other screens keep
              the "Weight Dec. Digits" setting.
  finalweight The end-of-shot screen and the shot log hold the same weight,
              locked 2 s after the motor stops, instead of the log taking the
              last steady reading before the portafilter is lifted. Apply after
              actualdose. Never flashed.

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

# logs: the /logs reader never wrapped its cursor at the end of the ring, so a
# drain ran off the end and overwrote RAM below its record buffer. logs_start
# picks the first record on a reset; logs_next moves the cursor, with wrapping,
# backwards for shots and forwards for the other logs.
LOGS_CAVE = 0x08183780
LOGS_STUB = bytes.fromhex(
    "29b95b681d3b03d503f5004300e01b68094a7047"                              # logs_start
    "094b1b7807490968084a52f8232003b94042401800d58018904228bf801a421a7047"  # logs_next
    "00bf"
    "68060524" "70060524" "78040024"                # cursor, log type, ring sizes
)
LOGS = [
    (0x0800DF68, bytes.fromhex("1b68274a"), thumb_branch(0x0800DF68, LOGS_CAVE, True)),
    (0x0800EB9C, bytes.fromhex("03461a46"), thumb_branch(0x0800EB9C, LOGS_CAVE + 0x14, True)),
    (0x0801D4CA, bytes.fromhex("d3f81530"), bytes.fromhex("d3f80d30")),  # ldr.w r3,[r3,#0xd]
    (0x0801D4D2, bytes.fromhex("b8ee644a"), bytes.fromhex("b8eee44a")),  # vcvt.f32.s32
    (0x08087B34, b"%.1fg, %.1fg, %.3fs, %.3fs", b"%.2fg, %.2fg, %.3fs, %.3fm"),
    (LOGS_CAVE, bytes(len(LOGS_STUB)), LOGS_STUB),
]

# laststat: six /stats counters (recipes 4-6) load from the record of the last
# shot instead. Each load becomes a bl to a stub that returns the value in r3.
LASTSTAT_CAVE = 0x081837C8
LASTSTAT_STUB = bytes.fromhex(
    "0b4bd3f801307047" "094bd3f805307047" "074bd3f809307047"   # ts, target, actual
    "054bd3f80d307047" "034bd3f811307047" "014b5b7e7047"       # disc, grind, flags
    "00bf" "a4aa0524"                                          # shot record
)
LASTSTAT = [
    (LASTSTAT_CAVE, bytes(len(LASTSTAT_STUB)), LASTSTAT_STUB),
]
for site, old, stub, fmt, was, now in (
    (0x0800E42A, "324b5b6a", 0x00, 0x080809E0, b'"r4Time"', b'"lastTs"'),
    (0x0800E44A, "2a4b9b6a", 0x08, 0x080809F0, b'"r5Time"', b'"lastTg"'),
    (0x0800E46A, "224bdb6a", 0x10, 0x08080A00, b'"r6Time"', b'"lastAc"'),
    (0x0800E54E, "b04bdb6b", 0x18, 0x08080A4C, b'"r4Weight"', b'"lastDisc"'),
    (0x0800E56E, "a84b1b6c", 0x20, 0x08080A60, b'"r5Weight"', b'"lastGrnd"'),
    (0x0800E58E, "a04b5b6c", 0x28, 0x08080A74, b'"r6Weight"', b'"lastFlag"'),
):
    LASTSTAT.append((site, bytes.fromhex(old), thumb_branch(site, LASTSTAT_CAVE + stub, True)))
    LASTSTAT.append((fmt, was, now))

# wifiboot: at boot the code logs in as owner and sets the Wi-Fi flag only if
# both keys are held. Rewritten in place so the flag is always set:
#   cbz r0,set_flag; ldr r3,=state; movs r2,#1; strb.w r2,[r3,#0xf5]; nops
WIFIBOOT = [
    (0x0800A2D8,
     bytes.fromhex("0346002b01f062834c4b012283f8f520"),
     bytes.fromhex("30b14e4b012283f8f52000bf00bf00bf")),
]

# The dose screen reads "Weight Dec. Digits" here; state 0x101 is the end of a
# weight-based shot. r3 holds the settings base on entry and the decimals on return.
ENDDEC_CAVE = 0x08183800
ENDDEC_STUB = bytes.fromhex("054a1288" "40f20111" "8a4202d0" "93f8ed30" "7047"
                            "0223" "7047" "00bf" "808f0524")
ENDDECIMALS = [
    (0x0804D59A, bytes.fromhex("93f8ed30"), thumb_branch(0x0804D59A, ENDDEC_CAVE, True)),
    (ENDDEC_CAVE, bytes(len(ENDDEC_STUB)), ENDDEC_STUB),
]

# finalweight: the shot record's weight is rewritten on every steady reading until
# the portafilter is lifted, which is also when the result screen closes. It now
# stops 2 s after the motor stops, and the screen shows the record. Before the first
# steady reading the record is 0 (cleared at the start of the grind), so the screen
# stays live. r3 holds the machine state on entry to both stubs.
FINAL_CAVE = 0x08183820
FINAL_STUB = bytes.fromhex(
    "094a" "d2f80920" "0ab9" "d3f89020" "7047"                          # weight to show, in r2
    "5a6d" "b2f5fa6f" "05d3" "044a" "d2f80920" "0ab1" "1346" "7047"     # weight to record, in r3
    "d3f89030" "7047"
    "a4aa0524"                                                          # shot record
)
FINALWEIGHT = [
    (0x0804D392, bytes.fromhex("d3f89020"), thumb_branch(0x0804D392, FINAL_CAVE, True)),
    (0x0800AFD4, bytes.fromhex("d3f89030"), thumb_branch(0x0800AFD4, FINAL_CAVE + 0x0E, True)),
    (FINAL_CAVE, bytes(len(FINAL_STUB)), FINAL_STUB),
]

PATCHES = {"actualdose": ACTUALDOSE, "ddd": DDD, "shotlog": SHOTLOG,
           "logs": LOGS, "laststat": LASTSTAT, "wifiboot": WIFIBOOT,
           "enddecimals": ENDDECIMALS, "finalweight": FINALWEIGHT}


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
