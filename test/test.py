# SPDX-FileCopyrightText: (c) 2026 Sritej Bommaraju
# SPDX-License-Identifier: Apache-2.0

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

CLOCK_PERIOD_US = 10

ADDR_BITS = 5
MEM_WORDS = 1 << ADDR_BITS
INDEX_BITS = 2
SETS = 1 << INDEX_BITS
WAYS = 2
LINES = SETS * WAYS
CNT_MAX = 31


def index_of(addr):
    """Which set an address maps to."""
    return addr % SETS

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

    def __init__(self, dut, bus, latency=MEM_LATENCY, ack_cycles=1, jitter=None):
        self.dut = dut
        self.bus = bus
        self.latency = latency
        self.ack_cycles = ack_cycles
        self.jitter = jitter  # rng for a randomly varying response time
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
                await self._delay()
                self.words[addr] = data
                self.writebacks += 1
                await self._ack()

            elif bit(uo, MEM_REQ):
                # Fill. The cache tells us which word it wants.
                addr = uo & 0x1F
                await self._delay()
                self.bus.data = self.words[addr]
                self.fills += 1
                await self._ack()

    async def _delay(self):
        cycles = self.jitter.randrange(0, 6) if self.jitter else self.latency
        if cycles:
            await ClockCycles(self.dut.clk, cycles)

    async def _ack(self):
        self.bus.pin7 = 1
        self.bus.apply()
        await ClockCycles(self.dut.clk, self.ack_cycles)
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
    # A read is answered on the bus; a write leaves it to the caller.
    expected_oe = 0x00 if write_data is not None else 0xFF
    assert int(dut.uio_oe.value) == expected_oe, (
        f"uio_oe was {int(dut.uio_oe.value):#04x}, expected {expected_oe:#04x}"
    )

    bus.start = 0
    bus.apply()
    await RisingEdge(dut.clk)
    return data, was_hit, was_miss, cycles


async def read(dut, bus, addr):
    return await access(dut, bus, addr)


async def write(dut, bus, addr, value):
    return await access(dut, bus, addr, write_data=value)


async def start_dut(dut, latency=MEM_LATENCY):
    cocotb.start_soon(Clock(dut.clk, CLOCK_PERIOD_US, unit="us").start())
    bus = await reset(dut)
    mem = Memory(dut, bus, latency=latency)
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
async def test_two_ways_coexist(dut):
    """Two addresses in one set now live side by side. A direct-mapped cache
    of the same capacity could not hold both."""
    bus, mem = await start_dut(dut)

    assert index_of(0) == index_of(4), "0 and 4 must share a set"

    await read(dut, bus, 0)
    await read(dut, bus, 4)

    data0, hit0, _, _ = await read(dut, bus, 0)
    data4, hit4, _, _ = await read(dut, bus, 4)

    assert hit0 == 1, "0 must survive the fetch of 4"
    assert hit4 == 1, "4 must be cached too"
    assert data0 == mem.words[0] and data4 == mem.words[4]


@cocotb.test()
async def test_third_address_in_set_evicts(dut):
    """Associativity raises the conflict threshold, it does not remove it.
    Three addresses in one set still do not fit in two ways."""
    bus, mem = await start_dut(dut)

    for addr in (0, 4, 8):
        assert index_of(addr) == 0

    await read(dut, bus, 0)
    await read(dut, bus, 4)
    await read(dut, bus, 8)

    # Check the survivor first. Probing the evicted address is itself a miss,
    # which would evict the survivor before we got to look at it.
    _, hit4, _, _ = await read(dut, bus, 4)
    assert hit4 == 1, "4 was used more recently and must survive"

    _, _, miss0, _ = await read(dut, bus, 0)
    assert miss0 == 1, "0 was the least recently used and must be gone"


@cocotb.test()
async def test_lru_evicts_least_recently_used(dut):
    """Touching a line protects it. The other way goes instead."""
    bus, mem = await start_dut(dut)

    await read(dut, bus, 0)
    await read(dut, bus, 4)
    await read(dut, bus, 0)  # 0 is now the most recently used of the pair
    await read(dut, bus, 8)  # so 4 is the one that should be evicted

    _, hit0, _, _ = await read(dut, bus, 0)
    assert hit0 == 1, "0 was touched most recently and must survive"

    _, _, miss4, _ = await read(dut, bus, 4)
    assert miss4 == 1, "4 was least recently used and must have been evicted"


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

    await write(dut, bus, 0, 0x77)  # dirty, in one way of set 0
    assert mem.words[0] != 0x77, "not written through"
    await read(dut, bus, 4)  # fills the other way of the same set

    writebacks_before = mem.writebacks
    await read(dut, bus, 8)  # a third address in the set forces an eviction

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
    await read(dut, bus, 4)  # fills the other way
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

    await read(dut, bus, 4)  # fills the other way of the set
    await read(dut, bus, 8)  # force address 0 out
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


# ---------------------------------------------------------------------------
# Regressions for bugs found by audit
# ---------------------------------------------------------------------------


@cocotb.test()
async def test_ack_is_edge_sensitive(dut):
    """ui_in[7] doubles as the counter select. If it is still high when a
    request misses, it must not be mistaken for a memory acknowledgement."""
    cocotb.start_soon(Clock(dut.clk, CLOCK_PERIOD_US, unit="us").start())
    bus = await reset(dut)

    # Deliberately no memory model. Nothing can legitimately complete a fill.
    bus.pin7 = 1     # as if the caller had just read the miss counter
    bus.data = 0xEE  # junk left on the bus
    bus.apply()
    await RisingEdge(dut.clk)

    bus.addr = 9
    bus.start = 1
    bus.apply()

    for cycle in range(20):
        await RisingEdge(dut.clk)
        if bit(int(dut.uo_out.value), READY):
            raise AssertionError(
                f"fill completed at cycle {cycle} with no memory present, "
                f"returning {int(dut.uio_out.value):#04x}"
            )

    assert bit(int(dut.uo_out.value), MEM_REQ) == 1, "cache should still be waiting"


@cocotb.test()
async def test_write_leaves_the_bus_to_the_caller(dut):
    """The caller drives its write data until it sees READY, so the cache must
    not drive the same pins at that moment."""
    bus, _ = await start_dut(dut)

    bus.addr = 6
    bus.we = 1
    bus.data = 0x3C
    bus.start = 1
    bus.apply()

    for _ in range(20):
        await RisingEdge(dut.clk)
        if bit(int(dut.uo_out.value), READY):
            oe = int(dut.uio_oe.value)
            assert oe == 0x00, (
                f"cache drove uio_oe={oe:#04x} while the caller still owns the bus"
            )
            return
    raise AssertionError("write never completed")


# ---------------------------------------------------------------------------
# Deeper correctness
# ---------------------------------------------------------------------------


@cocotb.test()
async def test_write_evicts_dirty_line(dut):
    """A write that misses onto a dirty victim has to write that victim back
    before installing itself. This path skips the fill, so it is its own case."""
    bus, mem = await start_dut(dut)

    await write(dut, bus, 0, 0x91)  # dirty in one way of set 0
    await write(dut, bus, 4, 0x92)  # dirty in the other way

    writebacks_before = mem.writebacks
    await write(dut, bus, 8, 0x93)  # third address: must evict a dirty line

    assert mem.writebacks == writebacks_before + 1, "dirty victim must be saved"
    assert mem.words[0] == 0x91, (
        f"evicted write landed wrong: memory[0] is {mem.words[0]:#04x}"
    )

    data, hit, _, _ = await read(dut, bus, 8)
    assert hit == 1 and data == 0x93, "the new line must be installed correctly"

    data, _, _, _ = await read(dut, bus, 4)
    assert data == 0x92, "the surviving dirty line must be intact"


@cocotb.test()
async def test_ack_held_high_acks_once(dut):
    """Memory that holds the acknowledgement high must not be read as two."""
    cocotb.start_soon(Clock(dut.clk, CLOCK_PERIOD_US, unit="us").start())
    bus = await reset(dut)
    mem = Memory(dut, bus, ack_cycles=5)
    cocotb.start_soon(mem.run())

    data, _, was_miss, _ = await read(dut, bus, 7)
    assert was_miss == 1
    assert data == mem.words[7], "a long acknowledgement must still fill correctly"

    data, hit, _, _ = await read(dut, bus, 7)
    assert hit == 1 and data == mem.words[7]


@cocotb.test()
async def test_zero_latency_memory(dut):
    """Memory that answers immediately must not race the handshake."""
    bus, mem = await start_dut(dut, latency=0)

    for addr in (2, 6, 10, 14):
        data, _, _, _ = await read(dut, bus, addr)
        assert data == mem.words[addr], f"addr {addr} wrong with instant memory"


@cocotb.test()
async def test_random_access_matches_reference(dut):
    """The cache must be invisible. Whatever sequence of reads and writes is
    thrown at it, a read returns the last value written to that address."""
    import random

    rng = random.Random(20260901)
    bus, mem = await start_dut(dut)

    reference = list(mem.words)

    for step in range(300):
        addr = rng.randrange(MEM_WORDS)
        if rng.random() < 0.4:
            value = rng.randrange(256)
            await write(dut, bus, addr, value)
            reference[addr] = value
        else:
            data, _, _, _ = await read(dut, bus, addr)
            assert data == reference[addr], (
                f"step {step}: read of addr {addr} returned {data:#04x}, "
                f"expected {reference[addr]:#04x}"
            )

    # Everything still cached must also be correct after being flushed out.
    for addr in range(MEM_WORDS):
        data, _, _, _ = await read(dut, bus, addr)
        assert data == reference[addr], (
            f"final sweep: addr {addr} returned {data:#04x}, "
            f"expected {reference[addr]:#04x}"
        )


@cocotb.test()
async def test_reset_invalidates_everything(dut):
    """Reset must clear the valid bits. Tags and data are deliberately not
    reset, so a stale tag surviving a reset would return junk as a hit."""
    bus, mem = await start_dut(dut)

    for addr in range(4):
        await read(dut, bus, addr)
        await write(dut, bus, addr, 0xF0 | addr)

    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)

    for addr in range(4):
        _, hit, was_miss, _ = await read(dut, bus, addr)
        assert was_miss == 1 and hit == 0, (
            f"addr {addr} hit after reset; valid bits were not cleared"
        )

    hits = await read_counter(dut, bus, select_miss=False)
    misses = await read_counter(dut, bus, select_miss=True)
    assert hits == 0, f"counters should have restarted, hit counter reads {hits}"
    assert misses == 4, f"expected 4 post-reset misses, counter reads {misses}"


@cocotb.test()
async def test_random_access_with_jittering_memory(dut):
    """Same reference check, but main memory answers after a random delay so
    the handshake cannot rely on a fixed response time."""
    import random

    rng = random.Random(31415)
    cocotb.start_soon(Clock(dut.clk, CLOCK_PERIOD_US, unit="us").start())
    bus = await reset(dut)
    mem = Memory(dut, bus, jitter=rng)
    cocotb.start_soon(mem.run())

    reference = list(mem.words)

    for step in range(300):
        addr = rng.randrange(MEM_WORDS)
        if rng.random() < 0.5:
            value = rng.randrange(256)
            await write(dut, bus, addr, value)
            reference[addr] = value
        else:
            data, _, _, _ = await read(dut, bus, addr)
            assert data == reference[addr], (
                f"step {step}: addr {addr} returned {data:#04x}, "
                f"expected {reference[addr]:#04x}"
            )

    for addr in range(MEM_WORDS):
        data, _, _, _ = await read(dut, bus, addr)
        assert data == reference[addr], f"final sweep: addr {addr} wrong"
