#!/usr/bin/env python3
"""Runs the grinder's own log and /stats code in an emulator.

Needs `pip install unicorn`. Loads a SW300 image and a FRAM image, then runs the
firmware's request parser and response builder unchanged. Only FRAM reads, the
CRC and the tick counter are replaced. No peripherals or interrupts are modelled,
so this checks the logic of the code, not its timing on the real grinder.
"""
import json
import struct

from unicorn import Uc, UcError
from unicorn.unicorn_const import UC_ARCH_ARM, UC_HOOK_CODE, UC_HOOK_MEM_INVALID, UC_MODE_THUMB
from unicorn.arm_const import (UC_ARM_REG_C1_C0_2, UC_ARM_REG_FPEXC, UC_ARM_REG_LR, UC_ARM_REG_PC,
                               UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_SP,
                               UC_CPU_ARM_CORTEX_A15)

FLASH = 0x08000000
DATA_IMAGE, DATA_RAM, DATA_SIZE = 0x181F58, 0x24000000, 0x17F4
RETURN = 0x08001000              # return address that stops the emulator
STACK_TOP = 0x2407F000

FRAM_READ = 0x080076C0
PARSER, BUILDER = 0x0800DBEC, 0x0800E014
STUBBED_ZERO = (0x08023040, 0x0800DB9C, 0x0802F508)  # CRC, CRC init, UART transmit
TICK = 0x08020CF8

RX, RX_LEN, PKT_OFF, TX = 0x2405B6B8, 0x2405B0B4, 0x24000460, 0x2405B0B8
RINGS = 0x240600FC               # FRAM 0x1000: 12 bytes per log (tail, head, count)
RECORD_BUFFER = 0x2405B078       # /logs reads each record here
LAST_SHOT = 0x2405AAA4
WIFI_FLAG, STATE = 0x24050618, 0x24058F80

SUB_LOGS, SUB_STATS = 0x0C, 0x0B


class Grinder:
    def __init__(self, image, fram):
        self.fram = fram
        self.tick = 0
        self.fault = None
        self.overruns = []       # (bytes below the record buffer, length) per bad copy
        mu = self.mu = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        mu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_A15)
        for base, size in ((0, 0x10000), (FLASH, 0x200000), (0x20000000, 0x20000),
                           (0x24000000, 0x80000), (0x30000000, 0x48000), (0x38000000, 0x10000),
                           (0x40000000, 0x20000000), (0x90000000, 0x1000000)):
            mu.mem_map(base, size)
        mu.mem_write(FLASH, bytes(image))
        mu.mem_write(DATA_RAM, bytes(image[DATA_IMAGE:DATA_IMAGE + DATA_SIZE]))
        mu.reg_write(UC_ARM_REG_C1_C0_2, mu.reg_read(UC_ARM_REG_C1_C0_2) | (0xF << 20))
        mu.reg_write(UC_ARM_REG_FPEXC, 0x40000000)
        mu.mem_write(RINGS, bytes(fram[0x1000:0x1030]))
        self.replace(FRAM_READ, self.fram_read)
        self.replace(TICK, self.next_tick)
        for addr in STUBBED_ZERO:
            self.replace(addr, lambda: 0)
        mu.hook_add(UC_HOOK_MEM_INVALID, self.bad_memory)

    def replace(self, addr, fn):
        """Run fn instead of the function at addr; its result becomes r0."""
        def hook(mu, _addr, _size, _user):
            value = fn()
            if value is not None:
                mu.reg_write(UC_ARM_REG_R0, value)
            if self.fault is None:
                mu.reg_write(UC_ARM_REG_PC, mu.reg_read(UC_ARM_REG_LR))
        self.mu.hook_add(UC_HOOK_CODE, hook, begin=addr, end=addr)

    def next_tick(self):
        self.tick += 1
        return self.tick

    def fram_read(self):
        mu = self.mu
        dst, src, n = (mu.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2))
        n = n - (1 << 32) if n & 0x80000000 else n
        if n > 0 and RECORD_BUFFER - 0x8000 <= dst < RECORD_BUFFER:
            self.overruns.append((RECORD_BUFFER - dst, n))
        if n > 0x800:            # the firmware's while(1) trap
            self.fault = f"fram_read trap: length {n}"
            mu.emu_stop()
        elif n > 0:
            mu.mem_write(dst, bytes(self.fram[src:src + n]))

    def bad_memory(self, mu, _access, addr, _size, _value, _user):
        self.fault = f"invalid memory {addr:#x} from pc {mu.reg_read(UC_ARM_REG_PC):#x}"
        return False

    def call(self, fn, stop=RETURN, count=20_000_000):
        mu = self.mu
        self.fault = None
        mu.reg_write(UC_ARM_REG_SP, STACK_TOP)
        mu.reg_write(UC_ARM_REG_LR, RETURN | 1)
        try:
            mu.emu_start(fn | 1, stop, count=count)
        except UcError as e:
            self.fault = self.fault or f"{e} at pc {mu.reg_read(UC_ARM_REG_PC):#x}"
        if mu.reg_read(UC_ARM_REG_PC) != stop and self.fault is None:
            self.fault = f"did not finish, pc {mu.reg_read(UC_ARM_REG_PC):#x}"

    def request(self, logtype=0, reset=0):
        """A log request, as the Wi-Fi module sends it, through the real parser."""
        self.mu.mem_write(RX, bytes([0x32, 3, 0, 0x0A, SUB_LOGS, logtype, reset, 0, 0]))
        self.mu.mem_write(RX_LEN, struct.pack("<H", 3))
        self.mu.mem_write(PKT_OFF, struct.pack("<I", 0))
        self.call(PARSER)

    def respond(self, sub):
        """Build one response frame and return its text."""
        self.mu.mem_write(RX, bytes([0x32, 3, 0, 0x0A, sub, 0, 0, 0, 0]))
        self.mu.mem_write(PKT_OFF, struct.pack("<I", 0))
        self.call(BUILDER)
        raw = bytes(self.mu.mem_read(TX + 4, 0x3FC))
        return raw.split(b"\0", 1)[0].decode("latin1")

    def drain(self, logtype=0, max_pages=400):
        """Read a whole log the way a client does. Stops at a fault."""
        self.request(logtype, reset=1)
        rows = []
        for _ in range(max_pages):
            text = self.respond(SUB_LOGS)
            if self.fault or not json.loads(text):
                break
            rows += json.loads(text)
            self.request(logtype, reset=0)
        return rows
