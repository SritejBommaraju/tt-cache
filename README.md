![](../../workflows/gds/badge.svg) ![](../../workflows/docs/badge.svg) ![](../../workflows/test/badge.svg)

# Tiny Cache

A 2-way set associative, write-back cache for the Tiny Tapeout IHP26b shuttle.

The cache sits between a caller and a main memory that lives off the chip, reached
over the pins. It holds 8 bytes in front of a 32 byte address space. A repeat
request for an address it already holds is answered in 3 clock cycles instead of 9,
and that gap widens as main memory gets slower.

- [Datasheet](docs/info.md)

## Design

| | |
| --- | --- |
| Organisation | 4 sets x 2 ways, 8 lines of one byte |
| Address space | 5 bits, 32 words |
| Address split | `addr[4:0] = tag[2:0] \| index[1:0]` |
| Replacement | LRU, one bit per set |
| Write policy | Write-back with a dirty bit, write-validate on a miss |
| Main memory | Off chip, over the pins |
| Instrumentation | 5-bit saturating hit and miss counters |

Both ways of a set are compared in parallel. A line is stored with a valid bit and
a dirty bit: valid says whether the line holds anything, dirty says whether the
cache holds the only correct copy.

Writes are not passed on to memory. A write hit updates the line and marks it
dirty, so ten writes to one address cost a single memory access rather than ten.
The cost is deferred to eviction: when a dirty line has to be reused, the cache
writes it back first, at the evicted line's own address.

A write that misses does not fetch the line first. With one word per line the
write covers the line completely, so the fetched word would be discarded
immediately.

Two addresses that map to the same set can both be cached. A third evicts the
least recently used of them, so associativity raises the conflict threshold
rather than removing it.

## Pinout

| Pin | Name | Meaning |
| --- | --- | --- |
| `ui[4:0]` | `ADDR` | Address being requested |
| `ui[5]` | `START` | Hold high to request an access |
| `ui[6]` | `WE` | 0 = read, 1 = write |
| `ui[7]` | `MEM_ACK` | Memory handshake, and the counter select while idle |
| `uo[7]` | `READY` | Access complete |
| `uo[6]` | `MEM_WE` | Writing an evicted line back to memory |
| `uo[5]` | `MEM_REQ` | Waiting for memory to supply a line |
| `uo[4:0]` | `MADDR` | Shared: memory address, or hit/miss, or a counter |
| `uio[7:0]` | `DATA` | Bidirectional data bus |

`READY`, `MEM_WE` and `MEM_REQ` are mutually exclusive and say what the low five
output pins currently mean. `MEM_ACK` is acted on when it rises, not while it is
high, because the same pin selects a counter when the cache is idle.

The data bus is shared by the caller and by main memory. The cache drives it only
while returning read data or writing a line back, and releases it otherwise.

## Running the tests

The testbench needs Python 3.13 or older, because cocotb 2.0.1 does not support
anything newer.

```sh
python3 -m venv .venv          # must be a Python <= 3.13 interpreter
source .venv/bin/activate
pip install -r test/requirements.txt

cd test
make -B
```

`make` exits 0 even when tests fail, so read the `TESTS= PASS= FAIL=` line rather
than the exit code. CI gates on `! grep failure results.xml` for the same reason.

To run the same tests against the post-synthesis netlist, copy the gate level
netlist to `test/gate_level_netlist.v` and run `make -B GATES=yes`.

## Tests

23 cocotb tests covering the read path, associativity and LRU ordering, the write
path including dirty eviction and writeback addressing, bus ownership, reset
invalidation, and the counters.

Two of them run 300 random reads and writes against a reference model and then
sweep all 32 addresses, one with a fixed memory latency and one with the memory
answering after a random delay. They assert a single property: a read returns the
last value written to that address.

## Status

| Check | Result |
| --- | --- |
| RTL simulation | 23 / 23 |
| Gate level simulation | 23 / 23 |
| LibreLane hardening | pass |
| Tiny Tapeout precheck | pass |
| Timing signoff | no setup or hold violations |

Hardened at 54% utilisation of a 1x1 tile: 14,568 um2, 833 cells, 152 flip-flops,
of which sequential elements are 51% of the area.

## Files

| Path | Contents |
| --- | --- |
| `src/project.v` | The cache |
| `src/config.json` | LibreLane configuration |
| `test/test.py` | cocotb tests and the main memory model |
| `test/tb.v` | Simulation wrapper |
| `test/Makefile` | RTL and gate level build |
| `info.yaml` | Submission manifest |
| `docs/info.md` | Datasheet |

## License

Apache 2.0. See [LICENSE](LICENSE).
