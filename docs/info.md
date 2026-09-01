<!---

This file is used to generate your project datasheet. Please fill in the information below and delete any unused
sections.

You can also include images in this folder and reference them in the markdown. Each image must be less than
512 kb in size, and the combined size of all images must be less than 1 MB.
-->

## How it works

This project is currently a placeholder while the design is under development.
The RTL implements a simple 8-bit combinational adder: the value on the
dedicated input bus is added to the value on the bidirectional input bus, and
the 8-bit sum is driven onto the dedicated output bus. The addition wraps on
overflow and there is no carry output. The bidirectional pins are configured as
inputs, so nothing is driven back out on them.

The design is purely combinational, so the clock and reset inputs are unused.

## How to test

Drive an 8-bit value onto `ui[7:0]` and a second 8-bit value onto `uio[7:0]`,
then read the sum back from `uo[7:0]`. For example, driving 20 on `ui` and 30
on `uio` produces 50 on `uo`.

Because the design is combinational, no clocking or reset sequence is required;
the output settles a propagation delay after the inputs change.

## External hardware

None. The design needs no external hardware beyond a way to drive the inputs
and observe the outputs.
