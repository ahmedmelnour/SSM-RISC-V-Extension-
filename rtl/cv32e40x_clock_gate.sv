// -----------------------------------------------------------------------------
// cv32e40x_clock_gate.sv
//
// FPGA-synthesizable replacement for vendor/cv32e40x/bhv/cv32e40x_sim_clock_gate.sv,
// which is simulation-only: it models the gate with an always_latch and would infer
// a latch on a gated clock net (unroutable / unusable on Xilinx fabric).
//
// On 7-series the correct primitive is BUFGCE -- a global clock buffer with a
// synchronous enable. The core instantiates this module by name from the sleep
// unit, so the module name and port list must match the vendor file exactly.
// The vendor bhv/ file is deliberately NOT added to the Vivado project.
// -----------------------------------------------------------------------------

module cv32e40x_clock_gate #(
  parameter LIB = 0
) (
  input  logic clk_i,
  input  logic en_i,
  input  logic scan_cg_en_i,
  output logic clk_o
);

  logic en;
  assign en = en_i | scan_cg_en_i;

  BUFGCE bufgce_i (
    .I (clk_i),
    .CE(en),
    .O (clk_o)
  );

endmodule
