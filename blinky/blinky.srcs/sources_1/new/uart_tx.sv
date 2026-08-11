`timescale 1ns / 1ps
//////////////////////////////////////////////////////////////////////////////////
// Company: 
// Engineer: 
// 
// Create Date: 08/10/2026 03:28:14 PM
// Design Name: 
// Module Name: uart_tx
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

module uart_tx #(
  parameter int CLK_FREQ_HZ = 50_000_000,
  parameter int BAUD_RATE   = 115_200
) (
  input  logic       clk_i,
  input  logic       rst_ni,
  input  logic       tx_valid_i,
  input  logic [7:0] tx_data_i,
  output logic       tx_busy_o,
  output logic       tx_o
);

  // Clocks per bit. 50 MHz / 115200 = 434.03 -> 434, giving ~0.16% error,
  // far inside the ~2% that 8N1 framing tolerates.
  localparam int CLKS_PER_BIT = CLK_FREQ_HZ / BAUD_RATE;
  localparam int CNT_W        = $clog2(CLKS_PER_BIT);

  logic [CNT_W-1:0] clk_cnt;
  logic [3:0]       bit_idx;   // 0 = start, 1..8 = data, 9 = stop
  logic [9:0]       shreg;     // {stop, data[7:0], start}
  logic             busy;

  assign tx_busy_o = busy;

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      clk_cnt <= '0;
      bit_idx <= '0;
      shreg   <= 10'h3FF;
      busy    <= 1'b0;
      tx_o    <= 1'b1;
    end else if (!busy) begin
      tx_o    <= 1'b1;
      clk_cnt <= '0;
      bit_idx <= '0;
      if (tx_valid_i) begin
        shreg <= {1'b1, tx_data_i, 1'b0};  // stop, data, start
        busy  <= 1'b1;
        tx_o  <= 1'b0;                     // start bit goes out immediately
      end
    end else begin
      if (clk_cnt == CNT_W'(CLKS_PER_BIT - 1)) begin
        clk_cnt <= '0;
        if (bit_idx == 4'd9) begin
          busy    <= 1'b0;                 // stop bit finished
          tx_o    <= 1'b1;
        end else begin
          bit_idx <= bit_idx + 4'd1;
          tx_o    <= shreg[bit_idx + 4'd1];
        end
      end else begin
        clk_cnt <= clk_cnt + CNT_W'(1);
      end
    end
  end

endmodule