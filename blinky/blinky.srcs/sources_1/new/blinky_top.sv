`timescale 1ns / 1ps
//////////////////////////////////////////////////////////////////////////////////
// Company: 
// Engineer: 
// 
// Create Date: 08/10/2026 02:27:21 PM
// Design Name: 
// Module Name: blinky_top
// Project Name: 
// Target Devices: 
// Tool Versions: 
// Description: 
// 
// Dependencies: 
// 
// Revision:
// Revision 0.01 - File Created
// Additional Comments:
// 
//////////////////////////////////////////////////////////////////////////////////

module blinky_top #(
  parameter int CLK_FREQ_HZ = 50_000_000,
  parameter int BAUD_RATE   = 115_200,
  parameter int GAP_BITS    = 22          // pause = 2**GAP_BITS cycles (~84 ms)
) (
  input  logic clk_i,       // 50 MHz oscillator, J19
  output logic uart_tx_o,   // -> CH340 RX, V2
  output logic led1_o       // M18, heartbeat
);

  // ---------------------------------------------------------------------------
  // Power-on reset: hold rst_n low for 65536 cycles (~1.3 ms) after
  // configuration. FPGA flip-flops power up to 0, so the counter starts at 0.
  // No reset pin is constrained -- nothing external is needed.
  // ---------------------------------------------------------------------------
  logic [15:0] por_cnt = 16'h0000;
  logic        rst_n;

  always_ff @(posedge clk_i) begin
    if (por_cnt != 16'hFFFF) por_cnt <= por_cnt + 16'd1;
  end
  assign rst_n = (por_cnt == 16'hFFFF);

  // ---------------------------------------------------------------------------
  // Heartbeat -- proves clock and configuration, nothing else.
  // bit[24] of a 50 MHz counter toggles every ~0.34 s.
  // ---------------------------------------------------------------------------
  logic [24:0] heartbeat_cnt;
  always_ff @(posedge clk_i or negedge rst_n) begin
    if (!rst_n) heartbeat_cnt <= '0;
    else        heartbeat_cnt <= heartbeat_cnt + 25'd1;
  end
  assign led1_o = heartbeat_cnt[24];

  // ---------------------------------------------------------------------------
  // Message ROM. A string literal is just an unsigned constant with the FIRST
  // character in the MOST significant bits, so index down from the top.
  // MSG_LEN must match the literal exactly: too small and the leading
  // characters are silently truncated, too large and it is zero-padded with
  // NULs that the terminal will not show.
  // ---------------------------------------------------------------------------
  localparam int MSG_LEN  = 15;                       // "A7-Lite alive" + CR + LF
  localparam int MSG_BITS = 8 * MSG_LEN;
  localparam logic [MSG_BITS-1:0] MSG = "A7-Lite alive\r\n";

  localparam int IDX_W = $clog2(MSG_LEN);

  logic [IDX_W-1:0]    idx;
  logic [GAP_BITS-1:0] gap_cnt;
  logic [7:0]          tx_data;
  logic                tx_valid;
  logic                tx_busy;

  assign tx_data = MSG[MSG_BITS-1 - 8*idx -: 8];

  typedef enum logic [1:0] { S_WAIT, S_PULSE, S_GAP } state_e;
  state_e state;

  always_ff @(posedge clk_i or negedge rst_n) begin
    if (!rst_n) begin
      state    <= S_WAIT;
      idx      <= '0;
      gap_cnt  <= '0;
      tx_valid <= 1'b0;
    end else begin
      tx_valid <= 1'b0;                    // default: single-cycle pulse

      unique case (state)
        // Wait for the transmitter to go idle, then arm a valid pulse.
        S_WAIT: if (!tx_busy) begin
          tx_valid <= 1'b1;
          state    <= S_PULSE;
        end

        // tx_valid is high for exactly this cycle, and idx is still pointing at
        // the byte being latched -- it only advances at the end of the cycle.
        S_PULSE: begin
          if (idx == IDX_W'(MSG_LEN - 1)) begin
            idx     <= '0;
            gap_cnt <= '0;
            state   <= S_GAP;
          end else begin
            idx   <= idx + IDX_W'(1);
            state <= S_WAIT;
          end
        end

        // Breathing room between banners so the line is not saturated.
        S_GAP: begin
          if (&gap_cnt) state   <= S_WAIT;
          else          gap_cnt <= gap_cnt + GAP_BITS'(1);
        end
      endcase
    end
  end

  uart_tx #(
    .CLK_FREQ_HZ (CLK_FREQ_HZ),
    .BAUD_RATE   (BAUD_RATE)
  ) u_uart_tx (
    .clk_i      (clk_i),
    .rst_ni     (rst_n),
    .tx_valid_i (tx_valid),
    .tx_data_i  (tx_data),
    .tx_busy_o  (tx_busy),
    .tx_o       (uart_tx_o)
  );

endmodule