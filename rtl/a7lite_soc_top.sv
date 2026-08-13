// -----------------------------------------------------------------------------
// a7lite_soc_top.sv
//
// Minimal SoC around CV32E40X for MicroPhase A7-Lite (XC7A35T-2FGG484) bring-up.
//
// Memory map
//   0x0000_0000 - 0x0001_FFFF   128 KB unified instruction/data RAM (BRAM)
//   0x1000_0000                 UART TX data   (write byte)
//   0x1000_0004                 UART status    (bit0 = busy, read-only)
//   0x1000_0008                 GPIO out       (bit0 -> LED2)
//
// Bring-up strategy -- three independent indicators, so a failure localises itself:
//   LED1 (M18)  free-running counter bit. Blinks if configuration + clock are alive.
//               Independent of the core entirely.
//   LED2 (N18)  driven by *software* through the GPIO register. Blinks only if the
//               core actually fetches, executes and stores. This proves the core is
//               running without depending on the UART pin being correct.
//   UART (V2)   full text output. Only this depends on the TX pin assignment.
//
// Reset is generated internally by a power-on counter. The A7-Lite manual refers to
// a "K3" reset key that does not appear in its own key table, so depending on a
// button here would mean guessing both a pin and a polarity. Nothing external is
// needed for the core to come out of reset.
//
// CV-X-IF is instantiated and tied off to "no coprocessor present" (X_EXT = 0) so
// the port exists and is correctly typed for the SSM accelerator phase later.
// -----------------------------------------------------------------------------

`include "fw_mem_path.svh"

module a7lite_soc_top
  import cv32e40x_pkg::*;
#(
  parameter logic [31:0] BOOT_ADDR   = 32'h0000_0000,
  parameter int          CLK_FREQ_HZ = 50_000_000,
  parameter int          BAUD_RATE   = 115_200
) (
  input  logic clk_i,       // 50 MHz oscillator, J19
  output logic uart_tx_o,   // -> CH340 RX
  output logic led1_o,      // M18, heartbeat
  output logic led2_o       // N18, software-driven
);

  // ---------------------------------------------------------------------------
  // Power-on reset: hold rst_n low for 65536 cycles (~1.3 ms at 50 MHz) after
  // configuration. FPGA flip-flops power up to 0, so the counter starts at 0.
  // ---------------------------------------------------------------------------
  logic [15:0] por_cnt = 16'h0000;
  logic        rst_n;

  always_ff @(posedge clk_i) begin
    if (por_cnt != 16'hFFFF) begin
      por_cnt <= por_cnt + 16'd1;
    end
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
  // Core <-> memory interfaces (OBI)
  // ---------------------------------------------------------------------------
  logic        instr_req, instr_gnt, instr_rvalid;
  logic [31:0] instr_addr, instr_rdata;

  logic        data_req, data_gnt, data_rvalid, data_we;
  logic [31:0] data_addr, data_wdata, data_rdata;
  logic [3:0]  data_be;

  logic [63:0] mcycle;
  logic        fencei_flush_req;

  // ---------------------------------------------------------------------------
  // CV-X-IF, tied off to "always reject"
  // ---------------------------------------------------------------------------
  cv32e40x_if_xif xif ();

  assign xif.compressed_ready       = 1'b0;
  assign xif.compressed_resp        = '0;

  assign xif.issue_ready            = 1'b0;
  assign xif.issue_resp             = '0;

  assign xif.mem_valid              = 1'b0;
  assign xif.mem_req                = '0;

  assign xif.result_valid           = 1'b0;
  assign xif.result                 = '0;

  // ---------------------------------------------------------------------------
  // CV32E40X core
  // ---------------------------------------------------------------------------
  cv32e40x_core #(
    .LIB              (0),
    .RV32             (RV32I),
    .A_EXT            (A_NONE),
    .B_EXT            (B_NONE),
    .M_EXT            (M),
    .DEBUG            (0),        // no debug module in this SoC
    .DBG_NUM_TRIGGERS (0),
    .PMA_NUM_REGIONS  (0),
    .CLIC             (0),
    .X_EXT            (0),
    .NUM_MHPMCOUNTERS (1)
  ) u_core (
    .clk_i               (clk_i),
    .rst_ni              (rst_n),
    .scan_cg_en_i        (1'b0),

    .boot_addr_i         (BOOT_ADDR),
    .dm_exception_addr_i (32'hF000_0000),
    .dm_halt_addr_i      (32'hF000_0000),
    .mhartid_i           (32'h0),
    .mimpid_patch_i      (4'h0),
    .mtvec_addr_i        (BOOT_ADDR),

    .instr_req_o         (instr_req),
    .instr_gnt_i         (instr_gnt),
    .instr_rvalid_i      (instr_rvalid),
    .instr_addr_o        (instr_addr),
    .instr_memtype_o     (),
    .instr_prot_o        (),
    .instr_dbg_o         (),
    .instr_rdata_i       (instr_rdata),
    .instr_err_i         (1'b0),

    .data_req_o          (data_req),
    .data_gnt_i          (data_gnt),
    .data_rvalid_i       (data_rvalid),
    .data_addr_o         (data_addr),
    .data_be_o           (data_be),
    .data_we_o           (data_we),
    .data_wdata_o        (data_wdata),
    .data_memtype_o      (),
    .data_prot_o         (),
    .data_dbg_o          (),
    .data_atop_o         (),
    .data_rdata_i        (data_rdata),
    .data_err_i          (1'b0),
    .data_exokay_i       (1'b1),

    .mcycle_o            (mcycle),
    .time_i              (64'h0),

    .xif_compressed_if   (xif.cpu_compressed),
    .xif_issue_if        (xif.cpu_issue),
    .xif_commit_if       (xif.cpu_commit),
    .xif_mem_if          (xif.cpu_mem),
    .xif_mem_result_if   (xif.cpu_mem_result),
    .xif_result_if       (xif.cpu_result),

    .irq_i               (32'h0),
    .wu_wfe_i            (1'b0),

    .clic_irq_i          (1'b0),
    .clic_irq_id_i       ('0),
    .clic_irq_level_i    ('0),
    .clic_irq_priv_i     ('0),
    .clic_irq_shv_i      (1'b0),

    .fencei_flush_req_o  (fencei_flush_req),
    .fencei_flush_ack_i  (fencei_flush_req),   // no I-cache: acknowledge immediately

    .debug_req_i         (1'b0),
    .debug_havereset_o   (),
    .debug_running_o     (),
    .debug_halted_o      (),
    .debug_pc_valid_o    (),
    .debug_pc_o          (),

    .fetch_enable_i      (1'b1),
    .core_sleep_o        ()
  );

  // ---------------------------------------------------------------------------
  // Unified 128 KB RAM, inferred as true dual-port BRAM.
  //   port A: instruction fetch (read only)
  //   port B: data load/store
  // 32768 words -> word index is addr[16:2], i.e. 15 bits.
  //
  // Sized at 128 KB (32 of the 50 RAMB36 on this part) so that an INT8 SSM model
  // of a few tens of thousands of parameters fits alongside code and stack. At
  // the original 32 KB a 20k-parameter model was already at the ceiling.
  //
  // The memory is written as four independent byte-wide arrays rather than one
  // 32-bit array with byte enables. With a single MEM_WORDSx32 array Vivado
  // reports
  //   [Synth 8-6841] ... cannot take advantage of ByteWide feature and is
  //   implemented with single write enable per RAM
  // because the word address exceeds its byte-write-enable threshold of 12 bits.
  // It then slices the RAM into 4-bit-wide primitives with one write enable
  // each, which cannot express a byte write at all -- so `sb`/`sh` would corrupt
  // the neighbouring bytes of the word. Splitting the lanes explicitly makes
  // each write enable a whole-RAM enable by construction, which is exactly what
  // the hardware supports, instead of depending on a `ram_decomp` attribute to
  // steer inference.
  //
  // The index width is derived from MEM_WORDS rather than written out, so the
  // array and the slice that addresses it cannot drift apart. An earlier,
  // abandoned SoC indexed an 8192-entry array with addr[16:2] and silently
  // aliased -- see doc/BRINGUP.md 10.2.
  // ---------------------------------------------------------------------------
  localparam int MEM_WORDS = 32768;
  localparam int AW_HI     = 2 + $clog2(MEM_WORDS) - 1;   // = 16
  localparam int IDX_W     = $clog2(MEM_WORDS);           // = 15

  (* ram_style = "block" *) logic [7:0] mem0 [0:MEM_WORDS-1];
  (* ram_style = "block" *) logic [7:0] mem1 [0:MEM_WORDS-1];
  (* ram_style = "block" *) logic [7:0] mem2 [0:MEM_WORDS-1];
  (* ram_style = "block" *) logic [7:0] mem3 [0:MEM_WORDS-1];

  initial begin
    $readmemh(`FW_MEM_B0, mem0);
    $readmemh(`FW_MEM_B1, mem1);
    $readmemh(`FW_MEM_B2, mem2);
    $readmemh(`FW_MEM_B3, mem3);
  end

  // ---- port A: instruction ----
  assign instr_gnt = instr_req;   // single-cycle address acceptance

  logic [IDX_W-1:0] ia;
  assign ia = instr_addr[AW_HI:2];

  always_ff @(posedge clk_i) begin
    if (instr_req) begin
      instr_rdata <= {mem3[ia], mem2[ia], mem1[ia], mem0[ia]};
    end
  end

  always_ff @(posedge clk_i or negedge rst_n) begin
    if (!rst_n) instr_rvalid <= 1'b0;
    else        instr_rvalid <= instr_req;
  end

  // ---- address decode ----
  logic sel_mem, sel_periph;
  assign sel_mem    = (data_addr[31:28] == 4'h0);
  assign sel_periph = (data_addr[31:28] == 4'h1);

  logic [31:0] mem_rdata;

  // ---- port B: data ----
  logic [IDX_W-1:0] da;
  assign da = data_addr[AW_HI:2];

  always_ff @(posedge clk_i) begin
    if (data_req && sel_mem) begin
      if (data_we && data_be[0]) mem0[da] <= data_wdata[7:0];
      if (data_we && data_be[1]) mem1[da] <= data_wdata[15:8];
      if (data_we && data_be[2]) mem2[da] <= data_wdata[23:16];
      if (data_we && data_be[3]) mem3[da] <= data_wdata[31:24];
      mem_rdata <= {mem3[da], mem2[da], mem1[da], mem0[da]};
    end
  end

  assign data_gnt = data_req;

  always_ff @(posedge clk_i or negedge rst_n) begin
    if (!rst_n) data_rvalid <= 1'b0;
    else        data_rvalid <= data_req;
  end

  // ---------------------------------------------------------------------------
  // Peripherals
  // ---------------------------------------------------------------------------
  logic       uart_busy;
  logic       uart_wr;
  logic       gpio_led;

  assign uart_wr = data_req && sel_periph && data_we && (data_addr[7:0] == 8'h00);

  uart_tx #(
    .CLK_FREQ_HZ (CLK_FREQ_HZ),
    .BAUD_RATE   (BAUD_RATE)
  ) u_uart_tx (
    .clk_i      (clk_i),
    .rst_ni     (rst_n),
    .tx_valid_i (uart_wr),
    .tx_data_i  (data_wdata[7:0]),
    .tx_busy_o  (uart_busy),
    .tx_o       (uart_tx_o)
  );

  always_ff @(posedge clk_i or negedge rst_n) begin
    if (!rst_n) begin
      gpio_led <= 1'b0;
    end else if (data_req && sel_periph && data_we && (data_addr[7:0] == 8'h08)) begin
      gpio_led <= data_wdata[0];
    end
  end
  assign led2_o = gpio_led;

  // ---------------------------------------------------------------------------
  // Read-data mux. mem_rdata and the peripheral read are both registered, so the
  // select must be registered too in order to line up with data_rvalid.
  // ---------------------------------------------------------------------------
  logic        sel_periph_q;
  logic [31:0] periph_rdata_q;

  always_ff @(posedge clk_i or negedge rst_n) begin
    if (!rst_n) begin
      sel_periph_q   <= 1'b0;
      periph_rdata_q <= 32'h0;
    end else begin
      sel_periph_q <= data_req && sel_periph;
      if (data_addr[7:0] == 8'h08) periph_rdata_q <= {31'h0, gpio_led};
      else                         periph_rdata_q <= {31'h0, uart_busy};
    end
  end

  assign data_rdata = sel_periph_q ? periph_rdata_q : mem_rdata;

endmodule
