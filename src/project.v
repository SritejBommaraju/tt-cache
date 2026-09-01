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
  // direct-mapped, write-back cache of 8 lines, one 8-bit word per line:
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
  localparam CNT_BITS   = 5;
  localparam CNT_MAX    = {CNT_BITS{1'b1}};

  // -------------------------------------------------------------------------
  // Request interface, unpacked from the dedicated input pins
  //
  // ui_in[7] carries two unrelated meanings, because there is no spare pin
  // and the two can never be needed at the same time:
  //   - while the cache is talking to memory it is the memory's handshake,
  //     meaning "fill data is on the bus" or "your writeback was accepted"
  //   - while the cache is idle it selects which counter to display
  // -------------------------------------------------------------------------
  wire [ADDR_BITS-1:0] req_addr  = ui_in[ADDR_BITS-1:0];
  wire                 req_start = ui_in[5];
  wire                 req_we    = ui_in[6];
  wire                 mem_ack   = ui_in[7];
  wire                 stat_sel  = ui_in[7];

  // -------------------------------------------------------------------------
  // Cache storage
  //
  // The tag and data arrays are deliberately not reset; a line is only ever
  // read when its valid bit is set, and the valid bits are reset.
  // -------------------------------------------------------------------------
  reg [TAG_BITS-1:0] tag_array   [0:LINES-1];
  reg                valid_array [0:LINES-1];
  reg                dirty_array [0:LINES-1];
  reg [7:0]          data_array  [0:LINES-1];

  // -------------------------------------------------------------------------
  // Latched request
  // -------------------------------------------------------------------------
  reg [ADDR_BITS-1:0] addr_q;
  reg                 we_q;
  reg [7:0]           wdata_q;

  wire [INDEX_BITS-1:0] index = addr_q[INDEX_BITS-1:0];
  wire [TAG_BITS-1:0]   tag   = addr_q[ADDR_BITS-1:INDEX_BITS];

  // The lookup: a line hits only if it holds valid data AND that data belongs
  // to the address we asked for.
  wire line_valid = valid_array[index];
  wire line_dirty = dirty_array[index];
  wire tag_match  = (tag_array[index] == tag);
  wire hit        = line_valid & tag_match;

  // A line must be pushed back to memory before it is reused only if it holds
  // something, and that something has been modified since it was fetched.
  wire needs_writeback = line_valid & line_dirty;

  // The address the evicted line belongs to, which is not the address being
  // requested. Main memory has no way to work this out, so we drive it out.
  wire [ADDR_BITS-1:0] wb_addr = {tag_array[index], index};

  // -------------------------------------------------------------------------
  // Control FSM
  // -------------------------------------------------------------------------
  localparam S_IDLE   = 3'd0;
  localparam S_LOOKUP = 3'd1;
  localparam S_WB     = 3'd2;
  localparam S_FILL   = 3'd3;
  localparam S_ALLOC  = 3'd4;
  localparam S_DONE   = 3'd5;

  reg [2:0] state;
  reg [7:0] data_q;
  reg       hit_q;
  reg       miss_q;

  reg [CNT_BITS-1:0] hit_count;
  reg [CNT_BITS-1:0] miss_count;

  integer i;

  always @(posedge clk) begin
    if (!rst_n) begin
      state      <= S_IDLE;
      addr_q     <= {ADDR_BITS{1'b0}};
      we_q       <= 1'b0;
      wdata_q    <= 8'h00;
      data_q     <= 8'h00;
      hit_q      <= 1'b0;
      miss_q     <= 1'b0;
      hit_count  <= {CNT_BITS{1'b0}};
      miss_count <= {CNT_BITS{1'b0}};
      for (i = 0; i < LINES; i = i + 1) begin
        valid_array[i] <= 1'b0;
        dirty_array[i] <= 1'b0;
      end
    end else begin
      case (state)

        // Wait for a request. Latch everything about it, so the rest of the
        // transaction is immune to the caller changing its mind.
        S_IDLE: begin
          hit_q  <= 1'b0;
          miss_q <= 1'b0;
          if (req_start) begin
            addr_q  <= req_addr;
            we_q    <= req_we;
            wdata_q <= uio_in;
            state   <= S_LOOKUP;
          end
        end

        S_LOOKUP: begin
          if (hit) begin
            hit_q <= 1'b1;
            if (hit_count != CNT_MAX) hit_count <= hit_count + 1'b1;
            if (we_q) begin
              // Write hit: update the line and mark it modified. Memory is
              // not told; that is what makes this a write-back cache.
              data_array[index]  <= wdata_q;
              dirty_array[index] <= 1'b1;
              data_q             <= wdata_q;
            end else begin
              data_q <= data_array[index];
            end
            state <= S_DONE;
          end else begin
            miss_q <= 1'b1;
            if (miss_count != CNT_MAX) miss_count <= miss_count + 1'b1;
            if (needs_writeback) begin
              state <= S_WB;
            end else if (we_q) begin
              // Write miss with nothing to evict. The write covers the whole
              // line, so there is no point fetching what we are about to
              // overwrite: install it directly.
              state <= S_ALLOC;
            end else begin
              state <= S_FILL;
            end
          end
        end

        // Push the modified line back to memory before its line is reused.
        S_WB: begin
          if (mem_ack) begin
            dirty_array[index] <= 1'b0;
            state              <= we_q ? S_ALLOC : S_FILL;
          end
        end

        // Wait for main memory. However many cycles this takes is the miss
        // penalty, and the reason the cache is worth having.
        S_FILL: begin
          if (mem_ack) begin
            data_array[index]  <= uio_in;
            tag_array[index]   <= tag;
            valid_array[index] <= 1'b1;
            dirty_array[index] <= 1'b0;
            data_q             <= uio_in;
            state              <= S_DONE;
          end
        end

        // Install a line straight from the write data, without reading memory.
        S_ALLOC: begin
          data_array[index]  <= wdata_q;
          tag_array[index]   <= tag;
          valid_array[index] <= 1'b1;
          dirty_array[index] <= 1'b1;
          data_q             <= wdata_q;
          state              <= S_DONE;
        end

        // Hold the result until the caller drops start, so a level-held
        // request cannot be mistaken for a second transaction.
        S_DONE: begin
          if (!req_start) state <= S_IDLE;
        end

        default: state <= S_IDLE;
      endcase
    end
  end

  // -------------------------------------------------------------------------
  // Outputs
  //
  // There are more things worth reporting than there are output pins, so the
  // low five are shared four ways, and the three status pins above them say
  // which meaning is live: the address during a memory transfer, the result
  // flags once ready, and otherwise one of the two counters.
  // -------------------------------------------------------------------------
  wire ready   = (state == S_DONE);
  wire mem_req = (state == S_FILL);
  wire mem_we  = (state == S_WB);

  // ready, mem_req and mem_we each get a pin of their own. They are mutually
  // exclusive, and between them they say unambiguously what the low five pins
  // currently mean - which matters because those five are shared three ways.
  wire [4:0] uo_low = mem_we  ? wb_addr
                    : mem_req ? addr_q
                    : ready   ? {3'b000, miss_q, hit_q}
                    :           (stat_sel ? miss_count : hit_count);

  assign uo_out = {ready, mem_we, mem_req, uo_low};

  // The cache drives the shared data bus only when it has something to put
  // there: the evicted word during a writeback, or the result once ready. At
  // every other time it lets go, which is what allows main memory and the
  // caller to use the very same pins.
  assign uio_out = mem_we ? data_array[index] : data_q;
  assign uio_oe  = (mem_we | ready) ? 8'hFF : 8'h00;

  // List all unused inputs to prevent warnings
  wire _unused = &{ena, 1'b0};

endmodule
