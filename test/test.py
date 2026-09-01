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

# uo_out bit positions
READY = 0
HIT = 1
MISS = 2
MEM_REQ = 3

# How many cycles main memory takes to answer. This is the miss penalty, and
# the whole reason the cache exists.
MEM_LATENCY = 4

# Main memory contents. Any deterministic function will do; this one just
# makes each word easy to recognise in a waveform.
MEMORY = [(addr * 7 + 3) & 0xFF for addr in range(MEM_WORDS)]


def pack_ui(addr=0, start=0, we=0, mem_valid=0):
    """Assemble the dedicated input pins into the single 8-bit ui_in bus."""
    return (addr & 0x1F) | (start << 5) | (we << 6) | (mem_valid << 7)


def bit(value, position):
    return (int(value) >> position) & 1


async def reset(dut):
    dut.ena.value = 1
    dut.ui_in.value = pack_ui()
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)


async def read(dut, addr, timeout=50):
    """Perform one cache read, playing the part of main memory on a miss.

    Returns (data, hit, miss, cycles).
    """
    dut.ui_in.value = pack_ui(addr=addr, start=1)

    cycles = 0
    served = False
    for _ in range(timeout):
        await RisingEdge(dut.clk)
        cycles += 1
        uo = int(dut.uo_out.value)

        # Act as main memory: once the cache asks for a line, wait out the
        # memory latency and then present the word alongside mem_valid.
        if bit(uo, MEM_REQ) and not served:
            await ClockCycles(dut.clk, MEM_LATENCY)
            cycles += MEM_LATENCY
            dut.uio_in.value = MEMORY[addr]
            dut.ui_in.value = pack_ui(addr=addr, start=1, mem_valid=1)
            served = True
            continue

        if bit(uo, READY):
            break
    else:
        raise AssertionError(f"read of addr {addr} never completed")

    uo = int(dut.uo_out.value)
    data = int(dut.uio_out.value)
    was_hit = bit(uo, HIT)
    was_miss = bit(uo, MISS)

    # The cache only drives the data bus while the result is valid.
    assert int(dut.uio_oe.value) == 0xFF, "cache should drive uio while ready"

    # Drop the request and let the cache return to idle.
    dut.ui_in.value = pack_ui(addr=addr, start=0)
    await RisingEdge(dut.clk)
    dut.uio_in.value = 0

    return data, was_hit, was_miss, cycles


@cocotb.test()
async def test_cold_miss_then_hit(dut):
    """A first touch always misses; the same address immediately after hits."""
    cocotb.start_soon(Clock(dut.clk, CLOCK_PERIOD_US, unit="us").start())
    await reset(dut)

    data, was_hit, was_miss, miss_cycles = await read(dut, 5)
    assert was_miss == 1 and was_hit == 0, "first access to an address must miss"
    assert data == MEMORY[5], f"got {data}, expected {MEMORY[5]}"

    data, was_hit, was_miss, hit_cycles = await read(dut, 5)
    assert was_hit == 1 and was_miss == 0, "second access to the same address must hit"
    assert data == MEMORY[5]

    assert hit_cycles < miss_cycles, (
        f"a hit ({hit_cycles} cycles) must be cheaper than a miss ({miss_cycles})"
    )
    dut._log.info(f"miss took {miss_cycles} cycles, hit took {hit_cycles}")


@cocotb.test()
async def test_all_lines_independent(dut):
    """Filling all 8 lines must not disturb one another."""
    cocotb.start_soon(Clock(dut.clk, CLOCK_PERIOD_US, unit="us").start())
    await reset(dut)

    for addr in range(LINES):
        _, _, was_miss, _ = await read(dut, addr)
        assert was_miss == 1, f"addr {addr} should be a cold miss"

    for addr in range(LINES):
        data, was_hit, _, _ = await read(dut, addr)
        assert was_hit == 1, f"addr {addr} should still be cached"
        assert data == MEMORY[addr]


@cocotb.test()
async def test_conflict_miss(dut):
    """Addresses sharing an index evict each other. This is the defining
    weakness of a direct-mapped cache."""
    cocotb.start_soon(Clock(dut.clk, CLOCK_PERIOD_US, unit="us").start())
    await reset(dut)

    # 0 and 8 differ only in their tag, so both map to index 0.
    assert (0 % LINES) == (8 % LINES)

    _, _, was_miss, _ = await read(dut, 0)
    assert was_miss == 1, "cold miss on 0"

    _, was_hit, _, _ = await read(dut, 0)
    assert was_hit == 1, "0 is now cached"

    # This evicts address 0 even though the cache has seven other free lines.
    _, _, was_miss, _ = await read(dut, 8)
    assert was_miss == 1, "cold miss on 8"

    data, was_hit, was_miss, _ = await read(dut, 0)
    assert was_miss == 1, "0 must have been evicted by 8 - they share an index"
    assert data == MEMORY[0], "the refetched data must still be correct"


@cocotb.test()
async def test_tag_isolation(dut):
    """Every address in memory returns its own word, never a neighbour's."""
    cocotb.start_soon(Clock(dut.clk, CLOCK_PERIOD_US, unit="us").start())
    await reset(dut)

    for addr in range(MEM_WORDS):
        data, _, _, _ = await read(dut, addr)
        assert data == MEMORY[addr], (
            f"addr {addr} returned {data}, expected {MEMORY[addr]}"
        )


@cocotb.test()
async def test_bus_is_released_when_idle(dut):
    """The cache must let go of the shared data bus unless it is answering."""
    cocotb.start_soon(Clock(dut.clk, CLOCK_PERIOD_US, unit="us").start())
    await reset(dut)

    assert int(dut.uio_oe.value) == 0x00, "idle cache must not drive uio"

    await read(dut, 1)
    await RisingEdge(dut.clk)
    assert int(dut.uio_oe.value) == 0x00, "cache must release uio after the access"
