# SPDX-FileCopyrightText: (c) 2026 Sritej Bommaraju
# SPDX-License-Identifier: Apache-2.0

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

CLOCK_PERIOD_US = 10

ADDR_BITS = 5
MEM_WORDS = 1 << ADDR_BITS
INDEX_BITS = 3
LINES = 1 << INDEX_BITS
CNT_MAX = 31

# uo_out bit positions. ready / mem_we / mem_req are dedicated; the low five
# pins mean different things depending on which of those three is asserted.
READY = 7
MEM_WE = 6
MEM_REQ = 5
HIT = 0    # only meaningful while READY
MISS = 1   # only meaningful while READY

# How long main memory takes to answer. This is the miss penalty, and the
# whole reason the cache exists.
MEM_LATENCY = 4


def golden_memory():
    """Initial contents of main memory. Any deterministic function will do."""
    return [(addr * 7 + 3) & 0xFF for addr in range(MEM_WORDS)]


def bit(value, position):
    return (int(value) >> position) & 1


class Bus:
    """Shared owner of the input pins.

    The test and the memory model both drive ui_in / uio_in, so they go
    through one object rather than fighting over the same handles.
    """

    def __init__(self, dut):
        self.dut = dut
        self.addr = 0
        self.start = 0
        self.we = 0
        self.pin7 = 0  # mem_ack during a memory transfer, stat_sel when idle
        self.data = 0
        self.apply()

    def apply(self):
        self.dut.ui_in.value = (
            (self.addr & 0x1F) | (self.start << 5) | (self.we << 6) | (self.pin7 << 7)
        )
        self.dut.uio_in.value = self.data & 0xFF


async def reset(dut):
    dut.ena.value = 1
    bus = Bus(dut)
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)
    return bus


class Memory:
    """Main memory, living outside the chip.

    Watches the cache's request pins and services them. Everything it needs -
    the address, and for a writeback the data - arrives on the pins, so it
    keeps no model of the cache at all.
    """

    def __init__(self, dut, bus, latency=MEM_LATENCY):
        self.dut = dut
        self.bus = bus
        self.latency = latency
        self.words = golden_memory()
        self.fills = 0
        self.writebacks = 0

    async def run(self):
        dut = self.dut
        while True:
            await RisingEdge(dut.clk)
            uo = int(dut.uo_out.value)

            if bit(uo, MEM_WE):
                # Eviction. The address and the data are both on the pins.
                addr = uo & 0x1F
                data = int(dut.uio_out.value)
                await ClockCycles(dut.clk, self.latency)
                self.words[addr] = data
                self.writebacks += 1
                await self._ack()

            elif bit(uo, MEM_REQ):
                # Fill. The cache tells us which word it wants.
                addr = uo & 0x1F
                await ClockCycles(dut.clk, self.latency)
                self.bus.data = self.words[addr]
                self.fills += 1
                await self._ack()

    async def _ack(self):
        self.bus.pin7 = 1
        self.bus.apply()
        await RisingEdge(self.dut.clk)
        self.bus.pin7 = 0
        self.bus.apply()


async def access(dut, bus, addr, write_data=None, timeout=200):
    """Run one cache transaction. Returns (data, hit, miss, cycles)."""
    bus.addr = addr
    bus.we = 1 if write_data is not None else 0
    if write_data is not None:
        bus.data = write_data
    bus.start = 1
    bus.apply()

    cycles = 0
    for _ in range(timeout):
        await RisingEdge(dut.clk)
        cycles += 1
        if bit(int(dut.uo_out.value), READY):
            break
    else:
        raise AssertionError(f"access to addr {addr} never completed")

    uo = int(dut.uo_out.value)
    data = int(dut.uio_out.value)
    was_hit = bit(uo, HIT)
    was_miss = bit(uo, MISS)
    assert int(dut.uio_oe.value) == 0xFF, "cache must drive uio while ready"

    bus.start = 0
    bus.apply()
    await RisingEdge(dut.clk)
    return data, was_hit, was_miss, cycles


async def read(dut, bus, addr):
    return await access(dut, bus, addr)


async def write(dut, bus, addr, value):
    return await access(dut, bus, addr, write_data=value)


async def start_dut(dut):
    cocotb.start_soon(Clock(dut.clk, CLOCK_PERIOD_US, unit="us").start())
    bus = await reset(dut)
    mem = Memory(dut, bus)
    cocotb.start_soon(mem.run())
    return bus, mem


async def read_counter(dut, bus, select_miss):
    """Counters are only exposed while the cache is idle.

    stat_sel feeds a combinational mux, so the pins have to be given an edge
    to settle on before they are sampled.
    """
    bus.pin7 = 1 if select_miss else 0
    bus.apply()
    await RisingEdge(dut.clk)
    assert bit(int(dut.uo_out.value), READY) == 0, "counters are only valid when idle"
    return int(dut.uo_out.value) & 0x1F


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------


@cocotb.test()
async def test_cold_miss_then_hit(dut):
    """A first touch always misses; the same address immediately after hits."""
    bus, mem = await start_dut(dut)

    data, was_hit, was_miss, miss_cycles = await read(dut, bus, 5)
    assert was_miss == 1 and was_hit == 0, "first access to an address must miss"
    assert data == mem.words[5]

    data, was_hit, was_miss, hit_cycles = await read(dut, bus, 5)
    assert was_hit == 1 and was_miss == 0, "second access to the same address must hit"
    assert data == mem.words[5]

    assert hit_cycles < miss_cycles, (
        f"a hit ({hit_cycles} cycles) must be cheaper than a miss ({miss_cycles})"
    )
    dut._log.info(f"miss took {miss_cycles} cycles, hit took {hit_cycles}")


@cocotb.test()
async def test_all_lines_independent(dut):
    """Filling all 8 lines must not disturb one another."""
    bus, mem = await start_dut(dut)

    for addr in range(LINES):
        _, _, was_miss, _ = await read(dut, bus, addr)
        assert was_miss == 1, f"addr {addr} should be a cold miss"

    for addr in range(LINES):
        data, was_hit, _, _ = await read(dut, bus, addr)
        assert was_hit == 1, f"addr {addr} should still be cached"
        assert data == mem.words[addr]


@cocotb.test()
async def test_conflict_miss(dut):
    """Addresses sharing an index evict each other. This is the defining
    weakness of a direct-mapped cache."""
    bus, mem = await start_dut(dut)

    assert (0 % LINES) == (8 % LINES), "0 and 8 must share an index"

    _, _, was_miss, _ = await read(dut, bus, 0)
    assert was_miss == 1, "cold miss on 0"

    _, was_hit, _, _ = await read(dut, bus, 0)
    assert was_hit == 1, "0 is now cached"

    # Evicts address 0 even though seven other lines are free.
    _, _, was_miss, _ = await read(dut, bus, 8)
    assert was_miss == 1, "cold miss on 8"

    data, _, was_miss, _ = await read(dut, bus, 0)
    assert was_miss == 1, "0 must have been evicted by 8 - they share an index"
    assert data == mem.words[0], "the refetched data must still be correct"


@cocotb.test()
async def test_tag_isolation(dut):
    """Every address in memory returns its own word, never a neighbour's."""
    bus, mem = await start_dut(dut)

    expected = list(mem.words)
    for addr in range(MEM_WORDS):
        data, _, _, _ = await read(dut, bus, addr)
        assert data == expected[addr], (
            f"addr {addr} returned {data}, expected {expected[addr]}"
        )


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------


@cocotb.test()
async def test_write_hit_then_read(dut):
    """A write to a cached line updates it, and is visible to the next read."""
    bus, mem = await start_dut(dut)

    await read(dut, bus, 3)  # pull the line in

    _, was_hit, _, _ = await write(dut, bus, 3, 0xAB)
    assert was_hit == 1, "writing an address already cached must hit"

    data, was_hit, _, _ = await read(dut, bus, 3)
    assert was_hit == 1
    assert data == 0xAB, f"read back {data:#04x}, expected 0xab"


@cocotb.test()
async def test_write_back_defers_memory_update(dut):
    """A write hit must NOT touch main memory. That is what write-back means."""
    bus, mem = await start_dut(dut)

    await read(dut, bus, 3)
    before = mem.words[3]
    writebacks_before = mem.writebacks

    await write(dut, bus, 3, 0xAB)

    assert mem.words[3] == before, "main memory must not be updated on a write hit"
    assert mem.writebacks == writebacks_before, "no writeback should have happened yet"


@cocotb.test()
async def test_write_miss_does_not_fetch(dut):
    """A write that covers the whole line has no reason to read memory first."""
    bus, mem = await start_dut(dut)

    fills_before = mem.fills
    _, was_hit, was_miss, _ = await write(dut, bus, 12, 0x5C)
    assert was_miss == 1, "writing an uncached address must miss"
    assert mem.fills == fills_before, (
        "a full-line write must not fetch the line it is about to overwrite"
    )

    data, was_hit, _, _ = await read(dut, bus, 12)
    assert was_hit == 1, "the line should now be cached"
    assert data == 0x5C


@cocotb.test()
async def test_dirty_eviction_writes_back(dut):
    """Evicting a modified line must push it to memory first, at its own
    address - not the address being fetched."""
    bus, mem = await start_dut(dut)

    await write(dut, bus, 0, 0x77)  # line 0 is now dirty
    assert mem.words[0] != 0x77, "not written through"

    writebacks_before = mem.writebacks
    await read(dut, bus, 8)  # same index, different tag: forces eviction

    assert mem.writebacks == writebacks_before + 1, "eviction must write back"
    assert mem.words[0] == 0x77, (
        f"writeback went to the wrong address: memory[0] is {mem.words[0]:#04x}"
    )

    data, _, was_miss, _ = await read(dut, bus, 0)
    assert was_miss == 1, "0 was evicted"
    assert data == 0x77, "the modified value must survive the round trip"


@cocotb.test()
async def test_clean_eviction_skips_writeback(dut):
    """An unmodified line can simply be dropped."""
    bus, mem = await start_dut(dut)

    await read(dut, bus, 0)  # clean
    writebacks_before = mem.writebacks
    await read(dut, bus, 8)  # evicts a clean line

    assert mem.writebacks == writebacks_before, (
        "a clean line must be discarded, not written back"
    )


@cocotb.test()
async def test_repeated_writes_evict_once(dut):
    """Many writes to one line cost one writeback, not one per write. This is
    the entire economic argument for write-back over write-through."""
    bus, mem = await start_dut(dut)

    writebacks_before = mem.writebacks
    for value in range(1, 11):
        await write(dut, bus, 0, value)

    assert mem.writebacks == writebacks_before, "still no eviction"

    await read(dut, bus, 8)  # force it out
    assert mem.writebacks == writebacks_before + 1, (
        "ten writes must produce exactly one writeback"
    )
    assert mem.words[0] == 10, "memory should hold only the final value"


# ---------------------------------------------------------------------------
# Bus discipline and counters
# ---------------------------------------------------------------------------


@cocotb.test()
async def test_bus_is_released_when_idle(dut):
    """The cache must let go of the shared data bus unless it is answering."""
    bus, _ = await start_dut(dut)

    assert int(dut.uio_oe.value) == 0x00, "idle cache must not drive uio"
    await read(dut, bus, 1)
    await RisingEdge(dut.clk)
    assert int(dut.uio_oe.value) == 0x00, "cache must release uio after the access"


@cocotb.test()
async def test_counters(dut):
    """Hit and miss counters, exposed on the low output pins while idle."""
    bus, _ = await start_dut(dut)

    assert await read_counter(dut, bus, select_miss=False) == 0
    assert await read_counter(dut, bus, select_miss=True) == 0

    for addr in range(4):  # 4 cold misses
        await read(dut, bus, addr)
    for addr in range(4):  # 4 hits
        await read(dut, bus, addr)

    assert await read_counter(dut, bus, select_miss=False) == 4, "expected 4 hits"
    assert await read_counter(dut, bus, select_miss=True) == 4, "expected 4 misses"


@cocotb.test()
async def test_counters_saturate(dut):
    """Counters stop at max rather than wrapping, so a wrap cannot look like
    a low count."""
    bus, _ = await start_dut(dut)

    await read(dut, bus, 0)
    for _ in range(CNT_MAX + 5):
        await read(dut, bus, 0)

    assert await read_counter(dut, bus, select_miss=False) == CNT_MAX, "hits must saturate"
