/*
 * Copyright (c) 2026 Sritej Bommaraju
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

module tt_um_sritejbommaraju_cache (
    input  wire [7:0] ui_in,    // Dedicated inputs
    output wire [7:0] uo_out,   // Dedicated outputs
    input  wire [7:0] uio_in,   // IOs: Input path
    output wire [7:0] uio_out,  // IOs: Output path
    output wire [7:0] uio_oe,   // IOs: Enable path (active high: 0=input, 1=output)
    input  wire       ena,      // always 1 when the design is powered, so you can ignore it
    input  wire       clk,      // clock
    input  wire       rst_n     // reset_n - low to reset
);

  // -------------------------------------------------------------------------
  // Geometry
  //
  // A 5-bit address space (32 words of main memory) in front of a
  // direct-mapped cache of 8 lines, one 8-bit word per line:
  //
  //     addr[4:0] = | tag[1:0] | index[2:0] |
  //
  // Eight lines needs three index bits, which leaves two tag bits. Four
  // different addresses therefore share each line and evict one another,
  // which is the defining behaviour of a direct-mapped cache.
  // -------------------------------------------------------------------------
  localparam ADDR_BITS  = 5;
  localparam INDEX_BITS = 3;
  localparam TAG_BITS   = ADDR_BITS - INDEX_BITS;
  localparam LINES      = 1 << INDEX_BITS;

  // -------------------------------------------------------------------------
  // Request interface, unpacked from the dedicated input pins
  // -------------------------------------------------------------------------
  wire [ADDR_BITS-1:0] req_addr  = ui_in[ADDR_BITS-1:0];
  wire                 req_start = ui_in[5];
  wire                 req_we    = ui_in[6];  // reserved for the write path
  wire                 mem_valid = ui_in[7];

  // -------------------------------------------------------------------------
  // Cache storage
  //
  // The tag and data arrays are deliberately not reset. The valid bits are,
  // and a line is only ever read when its valid bit is set, so resetting the
  // payload would cost flip-flop reset logic for no behavioural gain.
  // -------------------------------------------------------------------------
  reg [TAG_BITS-1:0] tag_array   [0:LINES-1];
  reg                valid_array [0:LINES-1];
  reg [7:0]          data_array  [0:LINES-1];

  // -------------------------------------------------------------------------
  // Latched request
  //
  // The address is captured on the cycle the request is accepted, so the
  // rest of the transaction is immune to the caller changing ui_in.
  // -------------------------------------------------------------------------
  reg [ADDR_BITS-1:0] addr_q;

  wire [INDEX_BITS-1:0] index = addr_q[INDEX_BITS-1:0];
  wire [TAG_BITS-1:0]   tag   = addr_q[ADDR_BITS-1:INDEX_BITS];

  // The lookup itself: a line hits only if it holds valid data AND that data
  // belongs to the address we asked for.
  wire line_valid = valid_array[index];
  wire tag_match  = (tag_array[index] == tag);
  wire hit        = line_valid & tag_match;

  // -------------------------------------------------------------------------
  // Control FSM
  // -------------------------------------------------------------------------
  localparam S_IDLE   = 2'd0;
  localparam S_LOOKUP = 2'd1;
  localparam S_FILL   = 2'd2;
  localparam S_DONE   = 2'd3;

  reg [1:0] state;
  reg [7:0] data_q;   // data handed back to the caller
  reg       hit_q;    // sticky result flags for this transaction
  reg       miss_q;

  integer i;

  always @(posedge clk) begin
    if (!rst_n) begin
      state  <= S_IDLE;
      addr_q <= {ADDR_BITS{1'b0}};
      data_q <= 8'h00;
      hit_q  <= 1'b0;
      miss_q <= 1'b0;
      for (i = 0; i < LINES; i = i + 1) begin
        valid_array[i] <= 1'b0;
      end
    end else begin
      case (state)

        // Wait for a request. Latch the address so the lookup is stable.
        S_IDLE: begin
          hit_q  <= 1'b0;
          miss_q <= 1'b0;
          if (req_start) begin
            addr_q <= req_addr;
            state  <= S_LOOKUP;
          end
        end

        // The tag comparison resolved combinationally while we were entering
        // this state, so the answer is available immediately.
        S_LOOKUP: begin
          if (hit) begin
            data_q <= data_array[index];
            hit_q  <= 1'b1;
            state  <= S_DONE;
          end else begin
            miss_q <= 1'b1;
            state  <= S_FILL;
          end
        end

        // Hold mem_req high until main memory presents the word. However many
        // cycles that takes is the miss penalty.
        S_FILL: begin
          if (mem_valid) begin
            data_array[index]  <= uio_in;
            tag_array[index]   <= tag;
            valid_array[index] <= 1'b1;
            data_q             <= uio_in;
            state              <= S_DONE;
          end
        end

        // Hold the result until the caller drops start, so a level-held
        // request cannot be mistaken for a second transaction.
        S_DONE: begin
          if (!req_start) begin
            state <= S_IDLE;
          end
        end

        default: state <= S_IDLE;
      endcase
    end
  end

  // -------------------------------------------------------------------------
  // Outputs
  // -------------------------------------------------------------------------
  wire ready   = (state == S_DONE);
  wire mem_req = (state == S_FILL);

  assign uo_out = {1'b0, (state != S_IDLE), state, mem_req, miss_q, hit_q, ready};

  // The data bus is only driven back at the caller once the transaction has
  // resolved. At every other time it is an input, which is what lets main
  // memory drive fill data onto the very same pins.
  assign uio_out = data_q;
  assign uio_oe  = ready ? 8'hFF : 8'h00;

  // List all unused inputs to prevent warnings
  wire _unused = &{ena, req_we, 1'b0};

endmodule
