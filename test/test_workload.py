# SPDX-FileCopyrightText: (c) 2026 Sritej Bommaraju
# SPDX-License-Identifier: Apache-2.0
#
# Hit rate under different access patterns. These measure how effective the
# cache is, which the correctness tests deliberately say nothing about.

import random

import cocotb
from cocotb.clock import Clock

from test import MEM_WORDS, SETS, WAYS, Memory, index_of, read, reset


async def measure(dut, addresses):
    """Run an address stream and return (hits, misses, hit_rate)."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="us").start())
    bus = await reset(dut)
    cocotb.start_soon(Memory(dut, bus).run())

    hits = misses = 0
    for addr in addresses:
        _, was_hit, was_miss, _ = await read(dut, bus, addr)
        hits += was_hit
        misses += was_miss
    return hits, misses, hits / (hits + misses)


def report(dut, name, hits, misses, rate):
    dut._log.info(f"{name:34s} {hits:4d} hits {misses:4d} misses   {rate*100:5.1f}%")


@cocotb.test()
async def workload_tight_loop(dut):
    """A loop over 4 addresses in different sets. Fits easily."""
    stream = [a for _ in range(25) for a in (0, 1, 2, 3)]
    h, m, r = await measure(dut, stream)
    report(dut, "tight loop, 4 addresses", h, m, r)
    assert r > 0.90, "a working set this small should almost always hit"


@cocotb.test()
async def workload_exactly_capacity(dut):
    """A loop over 8 addresses, one per line. Exactly fills the cache."""
    stream = [a for _ in range(15) for a in range(8)]
    h, m, r = await measure(dut, stream)
    report(dut, "loop over 8, exactly capacity", h, m, r)
    assert r > 0.85, "a working set equal to capacity should still hit"


@cocotb.test()
async def workload_thrash_one_set(dut):
    """Three addresses in one set, alternating. Two ways cannot hold three."""
    conflict = [a for a in range(MEM_WORDS) if index_of(a) == 0][:3]
    assert len(conflict) == 3
    stream = [a for _ in range(20) for a in conflict]
    h, m, r = await measure(dut, stream)
    report(dut, f"thrash one set {conflict}", h, m, r)
    assert r < 0.10, "cycling three addresses through two ways should miss constantly"


@cocotb.test()
async def workload_sequential_scan(dut):
    """A linear sweep of all 32 addresses, twice. The working set is four
    times the capacity, so nothing survives to be reused."""
    stream = list(range(MEM_WORDS)) * 2
    h, m, r = await measure(dut, stream)
    report(dut, "sequential scan of all 32", h, m, r)
    assert r < 0.10, "a scan larger than the cache gets no reuse"


@cocotb.test()
async def workload_uniform_random(dut):
    """Uniform random over all 32 addresses. No locality to exploit, so the
    hit rate should land near the fraction of memory the cache holds."""
    rng = random.Random(4242)
    stream = [rng.randrange(MEM_WORDS) for _ in range(200)]
    h, m, r = await measure(dut, stream)
    report(dut, "uniform random over 32", h, m, r)
    capacity_fraction = (SETS * WAYS) / MEM_WORDS
    dut._log.info(f"  cache holds {capacity_fraction*100:.0f}% of memory")


@cocotb.test()
async def workload_temporal_locality(dut):
    """A realistic shape: mostly a hot working set of 6, occasionally
    something cold. This is the pattern caches are actually built for."""
    rng = random.Random(99)
    hot = [0, 1, 2, 5, 6, 7]
    stream = [
        rng.choice(hot) if rng.random() < 0.85 else rng.randrange(MEM_WORDS)
        for _ in range(200)
    ]
    h, m, r = await measure(dut, stream)
    report(dut, "85% hot set of 6, 15% cold", h, m, r)
    assert r > 0.60, "strong temporal locality should pay off clearly"
