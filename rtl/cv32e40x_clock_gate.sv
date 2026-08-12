`timescale 1ns / 1ps
//////////////////////////////////////////////////////////////////////////////////
// Company: 
// Engineer: 
// 
// Create Date: 08/12/2026 10:27:00 AM
// Design Name: 
// Module Name: cv32e40x_clock_gate
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
module cv32e40x_clock_gate #(parameter LIB = 0) (
  input  logic clk_i,
  input  logic en_i,
  input  logic scan_cg_en_i,
  output logic clk_o
);
  logic en;
  assign en = en_i | scan_cg_en_i;
  BUFGCE bufgce_i (.I(clk_i), .CE(en), .O(clk_o));
endmodule