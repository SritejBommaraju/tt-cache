<!---

This file is used to generate your project datasheet. Please fill in the information below and delete any unused
sections.

You can also include images in this folder and reference them in the markdown. Each image must be less than
512 kb in size, and the combined size of all images must be less than 1 MB.
-->

## How it works

This is a direct-mapped cache. It sits between a caller and a main memory that
lives off-chip, and its job is to answer repeat requests for the same address
without paying the cost of going out to that memory again.

The cache holds 8 lines of one 8-bit word each, in front of a 5-bit (32 word)
address space. An address is split as:

    addr[4:0] = | tag[1:0] | index[2:0] |

The index selects one of the 8 lines. The tag records which of the 4 addresses
sharing that index is currently stored there, and a valid bit records whether
the line holds anything at all. A request hits only when the line is valid and
its tag matches; anything else is a miss.

On a miss the cache raises `MEM_REQ` and waits. Main memory answers by placing
the word on the data bus and raising `MEM_VALID`; the cache stores it, records
the tag, sets the valid bit, and returns the word. Every later request for that
address is answered from the line without touching memory.

Because the cache is direct-mapped, an address has exactly one line it is
allowed to occupy. Addresses 0, 8, 16 and 24 all map to index 0, so touching
them alternately evicts one another even while the other seven lines sit empty.
That is a conflict miss, and it is the characteristic weakness of this design.

The data bus is bidirectional. Main memory drives it while supplying fill data,
and the cache drives it only while `READY` is high; at all other times the cache
releases it.

## How to test

Drive an address on `ADDR[4:0]` and hold `START` high. Watch `READY`: when it
goes high the access has completed, `HIT` or `MISS` says which happened, and the
word is on the data bus. Drop `START` to return the cache to idle.

If `MEM_REQ` goes high, the cache has missed and is waiting for main memory.
Place the word for that address on the data bus and raise `MEM_VALID`.

The interesting sequence is: read an address (miss), read it again (hit, and
noticeably fewer cycles), read another address 8 apart (miss), then read the
first one again — it misses, because the second evicted it.

The cocotb testbench in `test/` does exactly this, and plays the part of main
memory with a configurable latency.

## External hardware

None. Main memory is modelled by whatever drives the pins; the testbench does
this in simulation.
