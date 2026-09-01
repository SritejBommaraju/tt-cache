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

  // 2-way set associative, write-back, 4 sets of 2 lines, one byte per line.
  // addr[4:0] = | tag[2:0] | index[1:0] |
  localparam ADDR_BITS  = 5;
  localparam INDEX_BITS = 2;
  localparam TAG_BITS   = ADDR_BITS - INDEX_BITS;
  localparam SETS       = 1 << INDEX_BITS;
  localparam LINES      = 2 * SETS;
  localparam CNT_BITS   = 5;
  localparam CNT_MAX    = {CNT_BITS{1'b1}};

  // ui_in[7] is the memory handshake during a transfer and the counter
  // select while idle. The two can never be needed at the same time.
  wire [ADDR_BITS-1:0] req_addr  = ui_in[ADDR_BITS-1:0];
  wire                 req_start = ui_in[5];
  wire                 req_we    = ui_in[6];
  wire                 mem_ack   = ui_in[7];
  wire                 stat_sel  = ui_in[7];

  // Lines are addressed as {way, index}. Tags and data are not reset because
  // a line is only read when its valid bit is set.
  reg [TAG_BITS-1:0] tag_array   [0:LINES-1];
  reg                valid_array [0:LINES-1];
  reg                dirty_array [0:LINES-1];
  reg [7:0]          data_array  [0:LINES-1];

  // One bit per set naming the way to replace next.
  reg lru [0:SETS-1];

  reg [ADDR_BITS-1:0] addr_q;
  reg                 we_q;
  reg                 way_q;
  reg [7:0]           wdata_q;

  wire [INDEX_BITS-1:0] index = addr_q[INDEX_BITS-1:0];
  wire [TAG_BITS-1:0]   tag   = addr_q[ADDR_BITS-1:INDEX_BITS];

  wire [INDEX_BITS:0] line0 = {1'b0, index};
  wire [INDEX_BITS:0] line1 = {1'b1, index};

  // Both ways are compared at once. That parallel search is what associativity
  // costs in silicon and what it buys in hit rate.
  wire hit0 = valid_array[line0] & (tag_array[line0] == tag);
  wire hit1 = valid_array[line1] & (tag_array[line1] == tag);
  wire hit  = hit0 | hit1;

  // Fill an empty way before evicting anything; otherwise follow the LRU bit.
  wire victim_way = !valid_array[line0] ? 1'b0
                  : !valid_array[line1] ? 1'b1
                  :                       lru[index];

  wire [INDEX_BITS:0] hit_line    = {hit1, index};
  wire [INDEX_BITS:0] victim_line = {victim_way, index};
  wire [INDEX_BITS:0] active_line = {way_q, index};

  wire needs_writeback = valid_array[victim_line] & dirty_array[victim_line];

  // The evicted line belongs to a different address than the one requested,
  // and only the stored tag knows which.
  wire [ADDR_BITS-1:0] wb_addr = {tag_array[active_line], index};

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
      way_q      <= 1'b0;
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
      for (i = 0; i < SETS; i = i + 1) begin
        lru[i] <= 1'b0;
      end
    end else begin
      case (state)

        // Latch the whole request so the rest of the transaction is immune
        // to the caller changing its mind.
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
            hit_q      <= 1'b1;
            way_q      <= hit1;
            lru[index] <= ~hit1;  // the way we did not touch becomes the victim
            if (hit_count != CNT_MAX) hit_count <= hit_count + 1'b1;
            if (we_q) begin
              // Write hit updates the line only. Memory is told at eviction.
              data_array[hit_line]  <= wdata_q;
              dirty_array[hit_line] <= 1'b1;
              data_q                <= wdata_q;
            end else begin
              data_q <= data_array[hit_line];
            end
            state <= S_DONE;
          end else begin
            miss_q <= 1'b1;
            way_q  <= victim_way;
            if (miss_count != CNT_MAX) miss_count <= miss_count + 1'b1;
            if (needs_writeback) begin
              state <= S_WB;
            end else if (we_q) begin
              // A write covers the whole line, so there is nothing worth
              // fetching before overwriting it.
              state <= S_ALLOC;
            end else begin
              state <= S_FILL;
            end
          end
        end

        // Push the modified line out before its slot is reused.
        S_WB: begin
          if (mem_ack) begin
            dirty_array[active_line] <= 1'b0;
            state                    <= we_q ? S_ALLOC : S_FILL;
          end
        end

        // However long this takes is the miss penalty.
        S_FILL: begin
          if (mem_ack) begin
            data_array[active_line]  <= uio_in;
            tag_array[active_line]   <= tag;
            valid_array[active_line] <= 1'b1;
            dirty_array[active_line] <= 1'b0;
            data_q                   <= uio_in;
            lru[index]               <= ~way_q;
            state                    <= S_DONE;
          end
        end

        // Install a line straight from the write data, without reading memory.
        S_ALLOC: begin
          data_array[active_line]  <= wdata_q;
          tag_array[active_line]   <= tag;
          valid_array[active_line] <= 1'b1;
          dirty_array[active_line] <= 1'b1;
          data_q                   <= wdata_q;
          lru[index]               <= ~way_q;
          state                    <= S_DONE;
        end

        // Hold the result until the caller drops start, so a level-held
        // request is not mistaken for a second transaction.
        S_DONE: begin
          if (!req_start) state <= S_IDLE;
        end

        default: state <= S_IDLE;
      endcase
    end
  end

  wire ready   = (state == S_DONE);
  wire mem_req = (state == S_FILL);
  wire mem_we  = (state == S_WB);

  // The low five pins are shared four ways. ready, mem_we and mem_req are
  // mutually exclusive and say which meaning is live.
  wire [4:0] uo_low = mem_we  ? wb_addr
                    : mem_req ? addr_q
                    : ready   ? {3'b000, miss_q, hit_q}
                    :           (stat_sel ? miss_count : hit_count);

  assign uo_out = {ready, mem_we, mem_req, uo_low};

  // The cache drives the shared bus only when it has something to put there.
  assign uio_out = mem_we ? data_array[active_line] : data_q;
  assign uio_oe  = (mem_we | ready) ? 8'hFF : 8'h00;

  // List all unused inputs to prevent warnings
  wire _unused = &{ena, 1'b0};

endmodule
