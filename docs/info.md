<!---

This file is used to generate your project datasheet. Please fill in the information below and delete any unused
sections.

You can also include images in this folder and reference them in the markdown. Each image must be less than
512 kb in size, and the combined size of all images must be less than 1 MB.
-->

## How it works

This is a direct-mapped, write-back cache. It sits between a caller and a main
memory that lives off-chip, and its job is to answer repeat requests without
paying the cost of reaching that memory again.

The cache holds 8 lines of one 8-bit word each, in front of a 5-bit (32 word)
address space. An address is split as:

    addr[4:0] = | tag[1:0] | index[2:0] |

The index selects one of the 8 lines. The tag records which of the 4 addresses
sharing that index is stored there. A valid bit records whether the line holds
anything, and a dirty bit records whether it has been modified since it was
fetched. A request hits only when the line is valid and its tag matches.

Reads that miss raise `MEM_REQ` with the wanted address on `MADDR`, and wait for
main memory to place the word on the data bus and pulse `MEM_ACK`.

Writes are held in the cache and not passed on, which is what makes this a
write-back cache: a write hit updates the line and sets its dirty bit, and
nothing is sent to memory. Ten writes to the same address cost one memory
access, not ten. The cost is deferred to eviction: when a line must be reused
and it is dirty, the cache first raises `MEM_WE` with the *evicted* line's
address on `MADDR` and its data on the bus. That address is not the address
being requested, and main memory has no way to derive it, which is why it is
driven out.

A write that misses does not fetch the line first. With one word per line the
write covers the line completely, so the fetched word would be discarded
immediately; the cache installs the line directly from the write data instead.

Because the cache is direct-mapped, an address has exactly one line it may
occupy. Addresses 0, 8, 16 and 24 all map to index 0, so touching them
alternately evicts one another even while the other seven lines sit empty.
That is a conflict miss, and it is the characteristic weakness of this design.

Two 5-bit saturating counters record hits and misses. They saturate rather than
wrap, so a large count can never be mistaken for a small one.

There are more things worth reporting than there are output pins, so `MADDR`
is shared four ways. `READY`, `MEM_WE` and `MEM_REQ` are mutually exclusive and
say which meaning is live:

| Condition | `uo[4:0]` carries |
| --- | --- |
| `MEM_WE` | address of the line being evicted |
| `MEM_REQ` | address of the line being fetched |
| `READY` | bit 0 = `HIT`, bit 1 = `MISS` |
| otherwise | hit count, or miss count if `MEM_ACK` is high |

## How to test

Drive an address on `ADDR[4:0]`, set `WE` for a write (with the data on the
bidirectional bus), and hold `START` high. When `READY` goes high the access has
completed: `uo[0]` says it hit, `uo[1]` says it missed, and for a read the word
is on the data bus. Drop `START` to return the cache to idle.

While `START` is held, watch the two memory pins. If `MEM_REQ` goes high, place
the word for the address on `MADDR` onto the data bus and pulse `MEM_ACK`. If
`MEM_WE` goes high, store the byte on the data bus at the address on `MADDR`,
then pulse `MEM_ACK`.

The sequences worth trying:

- read an address, then read it again - the second is a hit and takes fewer
  cycles
- read an address, then one 8 apart, then the first again - it misses, because
  the second evicted it
- write an address several times, then read something 8 apart - exactly one
  writeback happens, carrying only the final value
- while idle, read the hit counter on `uo[4:0]`, or the miss counter by raising
  `MEM_ACK`

The cocotb testbench in `test/` does all of this, and models main memory with a
configurable latency.

## External hardware

None. Main memory is modelled by whatever drives the pins; the testbench does
this in simulation.
