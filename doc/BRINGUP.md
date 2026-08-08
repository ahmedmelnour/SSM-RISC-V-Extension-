# CV32E40X on MicroPhase A7-Lite — complete bring-up walkthrough

A full account of getting an unmodified OpenHW Group CV32E40X RISC-V core running on a
MicroPhase A7-Lite (Artix-7 `XC7A35T-2FGG484`), from an empty directory to a core printing
over UART.

Every step gives both the **command line** actually used and the **Vivado GUI equivalent**,
because the two flows diverge in places that matter (notably: the GUI is project-mode only,
and there is no GUI equivalent at all for the software toolchain).

**Result:** core boots, executes, prints. 313 consecutive ticks from reset with zero gaps,
still counting past 2000 with no restart. 3733 LUTs (18%), 8 RAMB36, WNS +1.660 ns.

---

## Table of contents

1. [Why the survey came first](#1-why-the-survey-came-first)
2. [Establishing the board facts](#2-establishing-the-board-facts)
3. [Repository structure](#3-repository-structure)
4. [The RTL, and why it looks like this](#4-the-rtl-and-why-it-looks-like-this)
5. [The firmware](#5-the-firmware)
6. [The build flow](#6-the-build-flow)
7. [Full Vivado GUI walkthrough](#7-full-vivado-gui-walkthrough)
8. [Programming the board](#8-programming-the-board)
9. [Verifying it actually works](#9-verifying-it-actually-works)
10. [The two bugs found along the way](#10-the-two-bugs-found-along-the-way)
11. [Appendix A — complete command log](#appendix-a--complete-command-log)
12. [Appendix B — CLI to GUI cheat sheet](#appendix-b--cli-to-gui-cheat-sheet)
13. [Appendix C — troubleshooting by symptom](#appendix-c--troubleshooting-by-symptom)

---

## 1. Why the survey came first

Bring-up fails in boring ways: a missing license feature, a board that isn't enumerating, a
toolchain that isn't on `$PATH`. Each of those looks like a design bug if you discover it
halfway through. So everything that could hard-block the run was checked before a single
line of RTL was written.

### 1.1 Is the board actually connected?

```bash
lsusb
ls -l /dev/ttyUSB* /dev/ttyACM*
groups
ls /etc/udev/rules.d/
```

What came back, and what each line means:

```
Bus 003 Device 018: ID 0403:6014 Future Technology Devices International, Ltd FT232H
Bus 003 Device 020: ID 1a86:7523 QinHeng Electronics CH340 serial converter
crw-rw---- 1 root dialout 188, 0 /dev/ttyUSB0
```

- **FT232H (`0403:6014`)** is the on-board JTAG programmer.
- **CH340 (`1a86:7523`)** is the USB-serial back-channel — this becomes `/dev/ttyUSB0`.
- The port is group `dialout`, and `groups` confirmed membership, so **no `sudo` is needed**
  for serial access. Worth checking early: otherwise every capture silently fails with a
  permission error.
- `52-xilinx-ftdi-usb.rules` was already present in `/etc/udev/rules.d/`, so the JTAG cable
  is usable without root too.

Both devices are on **USB bus 003**. That detail comes back in §9 — JTAG traffic and serial
traffic contend, and it produces a symptom that looks like a hardware fault.

> **GUI equivalent:** none for `lsusb`. The Vivado-side check is
> **Flow → Open Hardware Manager → Open Target → Auto Connect**. If the board appears there,
> the FTDI side is fine. The GUI tells you nothing about the CH340 or serial permissions —
> for that you need a terminal program (§9).

### 1.2 Is the toolchain present?

```bash
which riscv32-unknown-elf-gcc riscv64-unknown-elf-gcc riscv-none-elf-gcc
ls /opt/Xilinx/
```

Found `riscv-none-elf-gcc` 13.2.0 under `~/tools/xpack-riscv-none-elf-gcc-13.2.0-2/bin`, and
Vivado 2026.1 at `/opt/Xilinx/2026.1`.

### 1.3 Does Vivado have a usable license?

This machine has a known trap: Vivado ML **Enterprise** enumerates **zero parts** if the
license file contains an `INCREMENT Vivado_Alveo_Package` line. It then restricts device
support to Alveo cards, `get_parts` returns 0, and `create_project -part xc7a35t...` fails
with *"Specified part could not be found"* — which looks exactly like missing board files.

```bash
grep -c "INCREMENT Vivado_Alveo_Package" ~/.Xilinx/*.lic
# → 0  (already stripped, good)
```

> **GUI equivalent:** **Help → Manage License** shows the detected features. The faster check
> is the Tcl Console: `puts [llength [get_parts -quiet]]`. If that prints `0`, the license is
> the problem, not your project. A healthy install here reports 289 parts.

### 1.4 Is the JTAG chain alive?

This is the one check that could genuinely have blocked everything, so it was done before
any design work:

```bash
bash ~/vivado_projects/check_jtag.sh
```

which runs, in Vivado batch mode:

```tcl
open_hw_manager
connect_hw_server -quiet
foreach x [get_hw_targets -quiet] {
  open_hw_target $x
  foreach d [get_hw_devices -quiet] {
    puts "device: $d IDCODE=[get_property IDCODE $d]"
  }
  close_hw_target
}
```

Result:

```
TARGETS=1
  target: localhost:3121/xilinx_tcf/Digilent/210241416548
    device: xc7a35t_0  IDCODE=00000011011000101101000010010011
```

`0x0362D093` is the XC7A35T IDCODE. Board alive, correct silicon.

> **GUI equivalent:** **Flow → Open Hardware Manager**, then **Open Target → Auto Connect**.
> The device tree shows `xc7a35t_0`. Right-click the device → **Properties** to see IDCODE.

### 1.5 Network and disk

```bash
git ls-remote https://github.com/openhwgroup/cv32e40x HEAD   # reachable
df -h ~                                                      # 91 GB free
```

---

## 2. Establishing the board facts

**The A7-Lite has no published Vivado board file.** This is permanent, not a setup fault —
unlike the MicroPhase Z7-Lite, it was never published to the Vivado Store. Consequences:

- Projects must be created by selecting the **part** directly, never a board.
- Every constraint must be hand-written from the reference manual.

There is no point searching the Vivado Store or fiddling with `board.repoPaths`.

### 2.1 Extracting the pinout from the manual

The authoritative source was a PDF already on the machine:

```bash
find ~ -maxdepth 5 -iname "*a7*lite*"
# → ~/A7-LITE Reference Manual — Microphase_FPGA_DOC V1.0 documentation.pdf

pdftotext -layout "A7-LITE Reference Manual — Microphase_FPGA_DOC V1.0 documentation.pdf" a7lite.txt
grep -n -iE "uart|ch340|led|key|reset|clk|clock|osc|MHz" a7lite.txt
```

The extracted tables:

| Signal | FPGA pin | Manual wording |
|---|---|---|
| `CLK_50M` | `J19` | 50 MHz active crystal oscillator, position U10 |
| `LED1` | `M18` | D6 |
| `LED2` | `N18` | D5 |
| `UART_TX` | `V2` | "UART data output" |
| `UART_RX` | `U2` | "UART data input" |
| `KEY1` | `AA1` | K1 |
| `KEY2` | `W1` | K2 |

> **GUI equivalent:** open the PDF in any viewer and read the tables. `pdftotext` was used only
> because it makes the tables greppable.

### 2.2 Two traps in the manual

These both cost the previous attempt real time, so they're worth stating plainly.

**Trap 1 — `U2` is a pin *and* a component designator.** The manual's flash section says the
QSPI part is at *"Position U2"*. That's a **component reference designator on the PCB**, and
has nothing to do with **FPGA ball `U2`**. Conflating them leads to assigning the UART
transmitter to the FPGA's *receive* pin.

The signal names are from the FPGA's point of view, so:
- `UART_TX` / `V2` = FPGA **output** → CH340's receiver. **This is the pin to drive.**
- `UART_RX` / `U2` = FPGA **input** ← CH340's transmitter.

Driving `U2` from the FPGA would put the FPGA output driver and the CH340 output driver on
the same net — contention, and a real risk to both parts. **Do not "just try U2" if the UART
is silent.**

**Trap 2 — the phantom reset key.** The manual's reset section says the board "provides a key
(K3) which can be used as a reset signal". But its own key table lists only `K1`/`KEY1`/`AA1`
and `K2`/`KEY2`/`W1`. There is no K3 in the table.

Rather than guess both a pin and a polarity, **this design uses no reset button at all** —
reset is generated on-chip by a power-on counter. One fewer unknown during bring-up.

---

## 3. Repository structure

Created fresh, nothing inherited from earlier attempts:

```bash
mkdir -p ~/a7lite-cv32e40x/{rtl,fw,constr,scripts,build,doc}
cd ~/a7lite-cv32e40x
git init
git clone --depth 1 https://github.com/openhwgroup/cv32e40x.git vendor/cv32e40x
```

```
a7lite-cv32e40x/
├── README.md                     project overview + verified results
├── .gitignore                    ignores build/, vendor/, firmware artefacts
│
├── rtl/                          RTL written for this project
│   ├── a7lite_soc_top.sv         top: core + BRAM + UART + GPIO + reset
│   ├── uart_tx.sv                8N1 transmitter
│   └── cv32e40x_clock_gate.sv    BUFGCE gate replacing the vendor sim model
│
├── fw/                           bring-up firmware
│   ├── crt0.S                    startup: sp/gp, .bss clear, call main
│   ├── main.c                    banner + tick counter + LED toggle
│   ├── link.ld                   single 32 KB RAM at 0x0
│   ├── build.sh                  compile → .bin → four byte-lane .mem images
│   └── (generated) firmware.elf/.bin/.map/.dis/.mem, firmware_b{0..3}.mem
│
├── constr/
│   └── a7lite.xdc                pin constraints from the reference manual
│
├── scripts/
│   ├── fetch_core.sh             clone upstream core at pinned revision
│   ├── build.tcl                 non-project synth → impl → bitstream
│   ├── program.tcl               JTAG configuration + DONE check
│   └── capture_uart.py           read CH340, classify OK/GARBAGE/SILENT
│
├── doc/
│   └── BRINGUP.md                this file
│
├── vendor/cv32e40x/              upstream core, pinned at d952cd6 (gitignored)
│
└── build/                        generated (gitignored)
    ├── a7lite_soc.bit            the bitstream
    ├── fw_mem_path.svh           generated `define`s for $readmemh paths
    ├── post_synth.dcp / post_route.dcp
    ├── post_synth_util.rpt / post_route_util.rpt
    ├── timing_summary.rpt / drc.rpt
    └── vivado_build.log / build_stdout.log
```

**Why `vendor/` is gitignored:** the core is cloned, not vendored into history.
`scripts/fetch_core.sh` reproduces it at the pinned revision `d952cd6`. Pinning matters — the
core's parameter list and CV-X-IF struct layout change between revisions, and
`a7lite_soc_top.sv` is written against this one.

### 3.1 Which vendor files to compile

```bash
cat vendor/cv32e40x/cv32e40x_manifest.flist
ls vendor/cv32e40x/rtl/*.sv | wc -l          # 44
ls vendor/cv32e40x/rtl/include/              # cv32e40x_pkg.sv
grep -rhn '`include' vendor/cv32e40x/rtl/    # (nothing)
```

So the compile list is simply:

- `vendor/cv32e40x/rtl/include/cv32e40x_pkg.sv` — **must be read first** (package)
- `vendor/cv32e40x/rtl/*.sv` — all 44 files, including the `cv32e40x_if_xif` interface
- `rtl/*.sv` — this project's three files

**`vendor/cv32e40x/bhv/` is deliberately excluded.** It holds simulation-only models,
including the clock gate discussed in §4.1.

---

## 4. The RTL, and why it looks like this

### 4.1 The clock gate must be replaced

The vendor file `bhv/cv32e40x_sim_clock_gate.sv` carries this header:

```
// !!! It must not be used for FPGA synthesis !!!
```

and implements the gate as:

```systemverilog
always_latch
  if (clk_i == 1'b0) clk_en <= en_i | scan_cg_en_i;
assign clk_o = clk_i & clk_en;
```

A latch driving a clock net through LUT logic — unusable on Xilinx fabric. The 7-series
primitive for this job is **`BUFGCE`**, a global clock buffer with synchronous enable.
`rtl/cv32e40x_clock_gate.sv` provides it with the **same module name and port list**, so the
core picks it up by name with no edit to vendor code:

```systemverilog
module cv32e40x_clock_gate #(parameter LIB = 0) (
  input logic clk_i, en_i, scan_cg_en_i, output logic clk_o
);
  logic en;
  assign en = en_i | scan_cg_en_i;
  BUFGCE bufgce_i (.I(clk_i), .CE(en), .O(clk_o));
endmodule
```

Confirmed in the synthesis log:

```
u_core/sleep_unit_i/core_clock_gate_i/bufgce_i
BUFGCE => BUFGCTRL: 1 instance
```

**This substitution is silent if it goes wrong** — you'd get a working-looking build with a
latch on a clock. So `build.tcl` asserts it (§6.2).

### 4.2 Reset: on-chip power-on counter

```systemverilog
logic [15:0] por_cnt = 16'h0000;
always_ff @(posedge clk_i)
  if (por_cnt != 16'hFFFF) por_cnt <= por_cnt + 16'd1;
assign rst_n = (por_cnt == 16'hFFFF);
```

FPGA flip-flops power up to 0 after configuration, so the counter starts at 0 and holds reset
low for 65536 cycles ≈ **1.31 ms at 50 MHz**. No pin, no polarity guess, nothing external.

### 4.3 Memory: four byte-wide arrays, not one 32-bit array

This is the single most important implementation detail — see §10.1 for the bug that forced
it. 32 KB unified instruction/data RAM, true dual-port:

```systemverilog
(* ram_style = "block" *) logic [7:0] mem0 [0:8191];   // bits  7:0
(* ram_style = "block" *) logic [7:0] mem1 [0:8191];   // bits 15:8
(* ram_style = "block" *) logic [7:0] mem2 [0:8191];   // bits 23:16
(* ram_style = "block" *) logic [7:0] mem3 [0:8191];   // bits 31:24

// port A — instruction fetch (read only)
always_ff @(posedge clk_i)
  if (instr_req) instr_rdata <= {mem3[ia], mem2[ia], mem1[ia], mem0[ia]};

// port B — data load/store, one write enable per lane
always_ff @(posedge clk_i)
  if (data_req && sel_mem) begin
    if (data_we && data_be[0]) mem0[da] <= data_wdata[7:0];
    if (data_we && data_be[1]) mem1[da] <= data_wdata[15:8];
    if (data_we && data_be[2]) mem2[da] <= data_wdata[23:16];
    if (data_we && data_be[3]) mem3[da] <= data_wdata[31:24];
    mem_rdata <= {mem3[da], mem2[da], mem1[da], mem0[da]};
  end
```

8192 words → word index is `addr[14:2]`, i.e. 13 bits.

### 4.4 The OBI memory interface

CV32E40X uses OBI: `req`/`gnt` address handshake, then `rvalid`/`rdata` response.

```systemverilog
assign instr_gnt = instr_req;      // accept the address immediately
always_ff @(posedge clk_i or negedge rst_n)
  if (!rst_n) instr_rvalid <= 1'b0;
  else        instr_rvalid <= instr_req;
```

Because BRAM reads are registered, data appears exactly one cycle after the address — which
is precisely when `rvalid` asserts. Same pattern on the data port.

### 4.5 Address decode and peripherals

| Address | Access | Function |
|---|---|---|
| `0x0000_0000`–`0x0000_7FFF` | rw | 32 KB unified RAM |
| `0x1000_0000` | w | UART TX data |
| `0x1000_0004` | r | UART status, bit 0 = busy |
| `0x1000_0008` | rw | GPIO out, bit 0 → LED2 |

The read mux needs care: `mem_rdata` and the peripheral read are both **registered**, so the
select must be registered too, or the mux would switch a cycle before the data arrives:

```systemverilog
always_ff @(posedge clk_i or negedge rst_n)
  if (!rst_n) sel_periph_q <= 1'b0;
  else        sel_periph_q <= data_req && sel_periph;

assign data_rdata = sel_periph_q ? periph_rdata_q : mem_rdata;
```

### 4.6 Three independent indicators

Deliberately designed so a failure localises itself instead of presenting as "nothing works":

| Indicator | Pin | Proves |
|---|---|---|
| LED1 heartbeat | `M18` | configuration + clock, **independent of the core** |
| LED2 software GPIO | `N18` | the core **fetches, executes and stores** |
| UART banner | `V2` | the UART pin and baud divisor are right |

LED2 is the important one: it makes "is the core running?" observable **without** depending on
the UART pin being correct. Given the `V2`/`U2` ambiguity, that decoupling is what would have
turned a silent board from a mystery into a one-line diagnosis.

### 4.7 CV-X-IF tied off

The interface is instantiated and driven to "always reject", with `X_EXT = 0`:

```systemverilog
cv32e40x_if_xif xif ();
assign xif.compressed_ready = 1'b0;  assign xif.compressed_resp = '0;
assign xif.issue_ready      = 1'b0;  assign xif.issue_resp      = '0;
assign xif.mem_valid        = 1'b0;  assign xif.mem_req         = '0;
assign xif.result_valid     = 1'b0;  assign xif.result          = '0;
```

The port exists and is correctly typed, ready for the accelerator phase, but no coprocessor
logic is generated.

---

## 5. The firmware

No Vivado involvement at all — this is plain `riscv-none-elf-gcc`.

### 5.1 Linker script

Single 32 KB RAM at `0x0`. `_start` must land at address 0, because that is `BOOT_ADDR`:

```
MEMORY { RAM (rwx) : ORIGIN = 0x00000000, LENGTH = 32K }
.text : { *(.text.init) *(.text .text.*) *(.rodata .rodata.*) } > RAM
_stack_top = ORIGIN(RAM) + LENGTH(RAM);      /* 0x8000 */
```

### 5.2 Startup

`crt0.S` sets `sp` and `gp`, clears `.bss`, calls `main`. The `gp` load needs relaxation
disabled, or the assembler rewrites it relative to `gp` itself — which isn't valid yet:

```asm
.option push
.option norelax
    la gp, __global_pointer$
.option pop
```

`mtvec` is tied to `BOOT_ADDR` (`0x0`), so **any trap re-enters `_start`**. That's deliberate:
`main` prints an incrementing counter, so a trap loop shows up as the count **restarting from
zero** rather than as a silent hang.

### 5.3 Build

```bash
./fw/build.sh
```

```bash
riscv-none-elf-gcc -march=rv32imc_zicsr -mabi=ilp32 -Os -ffreestanding \
    -nostdlib -nostartfiles -T link.ld crt0.S main.c -o firmware.elf
riscv-none-elf-objcopy -O binary firmware.elf firmware.bin
# then Python splits the binary into four byte-lane .mem files
```

Verification that the image is sane:

```bash
head -20 firmware.dis     # _start at 0x00000000, sp → 0x8000
head -1 firmware.mem      # 30401073   (csrw mie, zero)
for l in 0 1 2 3; do head -1 firmware_b$l.mem; done
# → 73 10 40 30   — correct little-endian lane split of 0x30401073
```

> **GUI equivalent: none.** Vivado does not compile C. The AMD GUI tool for this is **Vitis**,
> which would mean creating a platform from an exported XSA and a bare-metal application
> project. For a 400-byte bring-up firmware with a custom SoC and no board support package,
> that is far more machinery than the four-line `gcc` invocation above. Keep this part on the
> command line even if you do everything else in the GUI.

### 5.4 Getting firmware into the BRAM

The image is baked into the bitstream via `$readmemh` in an `initial` block. The path must
resolve at elaboration time, and Vivado's working directory differs between synthesis and
simulation — so `build.tcl` **generates** `build/fw_mem_path.svh`:

```systemverilog
`define FW_MEM_B0 "/home/ahmedelnour/a7lite-cv32e40x/fw/firmware_b0.mem"
...
```

which the top level includes:

```systemverilog
`include "fw_mem_path.svh"
...
initial begin
  $readmemh(`FW_MEM_B0, mem0);
  $readmemh(`FW_MEM_B1, mem1);
  $readmemh(`FW_MEM_B2, mem2);
  $readmemh(`FW_MEM_B3, mem3);
end
```

> **Changing firmware requires re-running both `fw/build.sh` and the Vivado build**, because
> the contents live in the bitstream.

> **GUI note:** in project mode this generated header doesn't exist. Either create
> `build/fw_mem_path.svh` by hand once with absolute paths, or replace the macros with literal
> absolute path strings in `$readmemh`. Absolute paths are the reliable choice in the GUI —
> relative paths resolve against the run directory, which is several levels below the project.

---

## 6. The build flow

### 6.1 One command

```bash
cd ~/a7lite-cv32e40x
vivado -mode batch -nojournal -log build/vivado_build.log -source scripts/build.tcl
```

**Non-project mode** was chosen deliberately: the whole build is reproducible from the repo,
with no `.xpr` state to drift out of sync with the sources. The GUI cannot open a non-project
flow as a project — see §7 if you want the GUI route.

The script, in order:

```tcl
# 1. generate the $readmemh path header
# 2. read sources — package FIRST
read_verilog -sv $root/vendor/cv32e40x/rtl/include/cv32e40x_pkg.sv
read_verilog -sv [lsort [glob $root/vendor/cv32e40x/rtl/*.sv]]
read_verilog -sv [lsort [glob $root/rtl/*.sv]]
read_xdc      $root/constr/a7lite.xdc

# 3. synthesise
synth_design -top a7lite_soc_top -part xc7a35tfgg484-2L \
  -include_dirs [list $outdir $root/vendor/cv32e40x/rtl/include]

# 4. latch tripwire (see below)
# 5. implement
opt_design; place_design; phys_opt_design; route_design

# 6. reports, timing gate, bitstream
write_bitstream -force $outdir/a7lite_soc.bit
```

### 6.2 Two gates that fail the build rather than shipping something broken

**Latch check** — catches the sim clock gate being picked up by mistake:

```tcl
set latches [get_cells -hier -quiet -filter {PRIMITIVE_SUBGROUP == latch}]
if {[llength $latches] > 0} { error "latch inference — check bhv/ is excluded" }
```

**Timing gate** — refuses to emit a bitstream that doesn't meet timing, so a failing build
can't later be misdiagnosed as a hardware problem:

```tcl
set wns [get_property SLACK [get_timing_paths -delay_type max]]
set whs [get_property SLACK [get_timing_paths -delay_type min]]
if {$wns < 0 || $whs < 0} { error "TIMING FAILED — not writing bitstream" }
```

Both passed:

```
INFO: latch check passed (0 latches)
INFO: WNS = 1.660 ns, WHS = 0.084 ns
INFO: bitstream written to build/a7lite_soc.bit
```

### 6.3 Results

| Resource | Used | Available | % | Why that number |
|---|---|---|---|---|
| Slice LUTs | 3733 | 20800 | 17.95% | lower than a 5082 baseline because `DEBUG=0` drops trigger logic |
| Slice Registers | 2304 | 41600 | 5.54% | |
| RAMB36 | 8 | 50 | 16% | 4 lanes × 8192×8 bits = 2 tiles each — confirms the lane split |
| DSP48E1 | 3 | 90 | 3.33% | the `M` extension multiplier |
| Bonded IOB | 4 | 250 | 1.6% | `clk_i`, `uart_tx_o`, `led1_o`, `led2_o` — no reset pin |
| BUFGCTRL | 2 | 32 | 6.25% | clock buffer + the BUFGCE core gate |

**Timing headroom is the number to remember: WNS +1.660 ns at a 20 ns period → Fmax ≈ 54.5 MHz,
about 9% margin.** The bare core is already close to the limit on this part. An accelerator on
the CV-X-IF port must stay off the critical path, or the clock comes down — which would
confound PPA comparisons unless the frequency is fixed deliberately up front.

---

## 7. Full Vivado GUI walkthrough

Everything above, done in the GUI instead. The GUI is **project mode only**, so this produces
an `.xpr` rather than a scripted flow.

### 7.1 Launch

```bash
source /opt/Xilinx/2026.1/Vivado/settings64.sh
vivado &
```

### 7.2 Create the project

1. **File → Project → New…** → *Next*
2. Project name `a7lite_soc`, location of your choice → *Next*
3. **RTL Project**, and **untick** "Do not specify sources at this time" → *Next*
4. **Add Sources** → *Add Files*, and add:
   - `vendor/cv32e40x/rtl/include/cv32e40x_pkg.sv`
   - all 44 files from `vendor/cv32e40x/rtl/*.sv`
   - `rtl/a7lite_soc_top.sv`, `rtl/uart_tx.sv`, `rtl/cv32e40x_clock_gate.sv`
   - **Do not add anything from `vendor/cv32e40x/bhv/`**
   - Set *File type* to **SystemVerilog** for all of them (Vivado sometimes guesses Verilog,
     which fails on `always_ff`/interfaces)
   - Tick **Copy sources into project** only if you want a snapshot; leaving it unticked keeps
     the GUI project tracking your repo files
5. **Add Constraints** → add `constr/a7lite.xdc` → *Next*
6. **Default Part** → select the **Parts** tab, *not* Boards.
   Filter: Family `Artix-7`, Package `fgg484`, Speed `-2L` → choose **`xc7a35tfgg484-2L`**
   > The **Boards** tab will not list the A7-Lite. That is expected — there is no board file.
7. *Next* → *Finish*

### 7.3 Post-creation settings

**Include paths** (needed for the package and the generated header):

- **Settings → General → Verilog options** (the `…` button next to *Verilog Include Files
  Search Paths*) → add:
  - `vendor/cv32e40x/rtl/include`
  - `build`  (where `fw_mem_path.svh` lives)

**Firmware header** — the GUI won't generate it. Create `build/fw_mem_path.svh` by hand:

```systemverilog
`define FW_MEM_B0 "/home/ahmedelnour/a7lite-cv32e40x/fw/firmware_b0.mem"
`define FW_MEM_B1 "/home/ahmedelnour/a7lite-cv32e40x/fw/firmware_b1.mem"
`define FW_MEM_B2 "/home/ahmedelnour/a7lite-cv32e40x/fw/firmware_b2.mem"
`define FW_MEM_B3 "/home/ahmedelnour/a7lite-cv32e40x/fw/firmware_b3.mem"
```

**Set the top module** — in the *Sources* pane, right-click `a7lite_soc_top` → **Set as Top**.
It should render in bold. If Vivado picked something else, elaboration will look bizarre.

### 7.4 Synthesise

- **Flow Navigator → Run Synthesis** → *OK*
- When it completes, choose **Open Synthesized Design**

**Latch check** — the GUI has no equivalent of the scripted assertion, so run this in the
**Tcl Console** with the synthesized design open:

```tcl
get_cells -hier -filter {PRIMITIVE_SUBGROUP == latch}
```

An empty result is what you want. Anything returned means a `bhv/` file got in.

**Utilisation** — **Reports → Report Utilization**, or the *Utilization* section of the
synthesized design summary.

### 7.5 Implement and generate the bitstream

- **Flow Navigator → Run Implementation**
- then **Generate Bitstream** (it will offer to run implementation first if you skip ahead)

**Timing** — three ways to read it:
- the **Design Runs** tab has WNS / WHS / TNS columns
- **Open Implemented Design → Reports → Timing → Report Timing Summary**
- Tcl Console: `get_property SLACK [get_timing_paths -delay_type max]`

Positive WNS and WHS are required. The GUI will happily generate a bitstream with negative
slack — unlike `build.tcl`, which refuses. Check it yourself.

### 7.6 Program

- **Flow Navigator → Open Hardware Manager**
- **Open Target → Auto Connect**
- right-click `xc7a35t_0` → **Program Device**
- bitstream path is pre-filled from the active run; confirm it ends in `a7lite_soc.bit`
- **Program**

**Verify DONE** — Tcl Console:

```tcl
get_property REGISTER.CONFIG_STATUS.BIT14_DONE_PIN [current_hw_device]
```

`1` means configuration succeeded.

> **`DONE = 1` does not prove your pin assignments are right.** It only proves the bitstream
> loaded. That is exactly the failure mode LED2 exists to rule out (§4.6).

### 7.7 Serial output

**Vivado has no serial terminal.** Options:

- **Vitis Serial Terminal** — in Vitis, *Window → Show View → Vitis Serial Terminal*, add a
  port at 115200 8N1
- **GTKTerm** — GUI, `sudo apt install gtkterm`, select `/dev/ttyUSB0`, 115200 8N1
- **PuTTY** — GUI, *Connection type: Serial*, `/dev/ttyUSB0`, speed 115200
- **`screen /dev/ttyUSB0 115200`** — terminal (exit with `Ctrl-A` then `K`)
- **`minicom -D /dev/ttyUSB0 -b 115200`**
- this repo's `scripts/capture_uart.py`, which additionally classifies the result (§9)

---

## 8. Programming the board

```bash
vivado -mode batch -source scripts/program.tcl
```

```tcl
open_hw_manager
connect_hw_server -quiet
open_hw_target [lindex [get_hw_targets -quiet] 0]
set dev [lindex [get_hw_devices -quiet] 0]
current_hw_device $dev
set_property PROGRAM.FILE $bit $dev
program_hw_devices $dev
refresh_hw_device -quiet $dev
puts "DONE = [get_property REGISTER.CONFIG_STATUS.BIT14_DONE_PIN $dev]"
```

Output:

```
INFO: device = xc7a35t_0  IDCODE = 00000011011000101101000010010011
INFO: [Labtools 27-3164] End of startup status: HIGH
INFO: DONE = 1
INFO: configuration OK
```

This is **volatile** configuration — it loads into SRAM and is lost on power cycle. Writing to
the QSPI flash for persistence is a separate step (`write_cfgmem` / **Add Configuration Memory
Device** in the GUI) and was not needed for bring-up.

---

## 9. Verifying it actually works

```bash
python3 scripts/capture_uart.py --seconds 8
```

The script deliberately distinguishes three outcomes, because they need different fixes:

| Result | Meaning | Fix |
|---|---|---|
| `OK` | expected banner found | — |
| `GARBAGE` | bytes arrived, mostly non-printable | right pin, **wrong baud** |
| `SILENT` | nothing at all | **wrong pin** |

It uses `termios` rather than `pyserial` so it needs nothing installed.

### 9.1 First capture

```
tick 92
tick 93
...
tick 201
RESULT: ASCII but banner 'CV32E40X' not seen (printable 97%)
```

The core was already running from the earlier programming, so the capture attached mid-stream
and missed the banner. Counter climbing monotonically = **not trapping**.

### 9.2 Capture across a reset

Start the capture first, then reprogram inside the window:

```bash
# terminal 1
python3 scripts/capture_uart.py --seconds 45
# terminal 2
vivado -mode batch -source scripts/program.tcl
```

```
tick 478
tick 479

=====================================
 CV32E40X alive on MicroPhase A7-Lite
 RV32IMC @ 50 MHz, 32KB BRAM
=====================================
tick 0
tick 1
tick 2
...
```

Banner present, counter restarts cleanly at 0.

### 9.3 Checking for dropped data

The head of that capture showed **skipping** tick numbers (185, 188, 190, 191…), which would
be alarming if left uninvestigated. Quantified:

```
PRE-reprogram  : 284 ticks, 185..479, gaps=10
POST-reprogram : 313 ticks, 0..312,   gaps=0
pre gap span   : (185,188) ... (209,211)
```

**All ten gaps fall inside ticks 185–211 — exactly the window when Vivado was driving JTAG.**
The FT232H and the CH340 share **USB bus 003** (§1.1), so programming traffic starves the
serial reader host-side. Once programming finished, 313 consecutive ticks with **zero** gaps.

This is a host-side artefact, not a design fault. A capture taken on its own is gap-free.

### 9.4 Sustained run

A later check showed ticks 2024–2029 with no restart, confirming the core executes for
thousands of iterations without trapping.

---

## 10. The two bugs found along the way

### 10.1 Byte write-enables were being silently dropped

**Symptom** — during the first synthesis run:

```
WARNING: [Synth 8-6841] Block RAM (mem_reg) originally specified as a Byte Wide Write
Enable RAM cannot take advantage of ByteWide feature and is implemented with single write
enable per RAM due to following reason.
(address width (13) is more than optimal threshold of 12. Implementing using BWWE will
require more logic and timing would be suboptimal. Please use attribute ram_decomp = power
if BWWE is desired.)
```

**Why it matters** — 8192 words needs a 13-bit address, one bit over Vivado's threshold.
Vivado therefore slices the RAM into **4-bit-wide primitives with one write enable each**. A
4-bit slice is half a byte, so a byte write **cannot be expressed at all** — every `sb` would
write all four byte lanes and corrupt the rest of the word.

**Would it have bitten?** Yes. `uart_putdec` builds its digits in `char buf[11]` on the stack,
which compiles to `sb`. The banner would likely still have printed, and the counter would have
produced subtly wrong digits — a genuinely confusing failure.

**Fix** — rather than rely on the `ram_decomp = power` attribute Vivado suggested (steering
inference, still fragile), the memory was rewritten as **four independent byte-wide arrays**.
Each write enable is then a whole-RAM enable *by construction*, which is exactly what the
hardware supports. Nothing is left to inference heuristics.

**Confirmed fixed** — the warning disappeared, and utilisation shows exactly **8 RAMB36**:
4 lanes × 8192×8 bits = 65536 bits per lane = 2 tiles per lane. Precisely as intended.

The first build was killed mid-flight once this was spotted, rather than allowing a
functionally suspect bitstream to be produced:

```bash
pkill -f "vivado.*build.tcl"
```

### 10.2 The UART pin had been wrongly abandoned

The previous attempt's constraints file contained:

```
## Trying U2 instead since V2 produced no visible output.
set_property PACKAGE_PIN U2 [get_ports uart_tx_o]
```

But that project's top level also had:

- `rst_n` used at line 35, **declared at line 54** — used 19 lines before it exists
- `led2_o` constrained in the XDC but **absent from the module port list**
- the memory indexed with `addr[16:2]` (15 bits = 32768 entries) into an **8192-entry array**

So `V2` was never fairly tested — the design was broken independently of the pin.

`V2` is correct, confirmed empirically. And as established in §2.2, "just trying `U2`" is not
a safe fallback: `U2` is `UART_RX`, an FPGA input driven by the CH340, so driving it from the
FPGA creates bus contention.

---

## Appendix A — complete command log

### Survey

```bash
lsusb
ls -l /dev/ttyUSB* /dev/ttyACM*
groups
ls /etc/udev/rules.d/
which riscv32-unknown-elf-gcc riscv64-unknown-elf-gcc riscv-none-elf-gcc
ls /opt/Xilinx/
grep -c "INCREMENT Vivado_Alveo_Package" ~/.Xilinx/*.lic
find ~ -maxdepth 5 -iname "*a7*lite*"
git ls-remote https://github.com/openhwgroup/cv32e40x HEAD
df -h ~
bash ~/vivado_projects/check_jtag.sh
```

### Board documentation

```bash
pdftotext -layout "A7-LITE Reference Manual — Microphase_FPGA_DOC V1.0 documentation.pdf" a7lite.txt
grep -n -iE "uart|ch340|rxd|txd|led|key|reset|clk|clock|osc|MHz|bank" a7lite.txt
```

### Project setup

```bash
mkdir -p ~/a7lite-cv32e40x/{rtl,fw,constr,scripts,build,doc}
cd ~/a7lite-cv32e40x
git init
git config user.email "ahmedmelnourm@gmail.com"
git config user.name  "Ahmed Elnour"
git clone --depth 1 https://github.com/openhwgroup/cv32e40x.git vendor/cv32e40x
git -C vendor/cv32e40x rev-parse --short HEAD          # d952cd6

cat vendor/cv32e40x/cv32e40x_manifest.flist
ls vendor/cv32e40x/rtl/*.sv | wc -l
ls vendor/cv32e40x/rtl/include/
grep -rhn '`include' vendor/cv32e40x/rtl/
cat vendor/cv32e40x/bhv/cv32e40x_sim_clock_gate.sv
```

### Firmware

```bash
chmod +x fw/build.sh
./fw/build.sh
head -30 fw/firmware.dis
wc -l fw/firmware_b*.mem
head -1 fw/firmware.mem                                 # 30401073
for l in 0 1 2 3; do head -1 fw/firmware_b$l.mem; done  # 73 10 40 30
```

### Build

```bash
vivado -mode batch -nojournal -log build/vivado_build.log -source scripts/build.tcl

# inspect while running / afterwards
grep -E "8-6841|latch check|ERROR|CRITICAL WARNING" build/build_stdout.log
grep -E "Slice LUTs|Block RAM Tile|DSPs|Bonded IOB|BUFGCTRL" build/post_synth_util.rpt

# abort a run
pkill -f "vivado.*build.tcl"
```

### Program and verify

```bash
vivado -mode batch -source scripts/program.tcl
python3 scripts/capture_uart.py --seconds 8
python3 scripts/capture_uart.py --seconds 45   # while reprogramming, to catch the banner
```

### Version control

```bash
git add -A
git commit -m "..."
git log --oneline
git status --short
```

---

## Appendix B — CLI to GUI cheat sheet

| Command line | Vivado GUI |
|---|---|
| `bash check_jtag.sh` | Flow → Open Hardware Manager → Open Target → Auto Connect |
| `get_parts` returns 0 | Help → Manage License; or Tcl Console `llength [get_parts]` |
| `create_project -part xc7a35tfgg484-2L` | File → Project → New → **Parts** tab (not Boards) |
| `read_verilog -sv …` | Add Sources → Add or create design sources |
| `-include_dirs …` | Settings → General → Verilog options → Include Files Search Paths |
| `read_xdc` | Add Sources → Add or create constraints |
| `-top a7lite_soc_top` | Sources pane → right-click module → Set as Top |
| `synth_design` | Flow Navigator → Run Synthesis |
| latch assertion in `build.tcl` | Tcl Console: `get_cells -hier -filter {PRIMITIVE_SUBGROUP == latch}` |
| `report_utilization` | Open Synthesized/Implemented Design → Reports → Report Utilization |
| `opt/place/phys_opt/route_design` | Flow Navigator → Run Implementation |
| `report_timing_summary` | Design Runs tab (WNS column), or Reports → Report Timing Summary |
| timing gate in `build.tcl` | **no equivalent** — GUI will build with negative slack; check manually |
| `write_bitstream` | Flow Navigator → Generate Bitstream |
| `program_hw_devices` | Hardware Manager → right-click device → Program Device |
| `get_property …BIT14_DONE_PIN` | Hardware device Properties, or the same Tcl in the Console |
| `riscv-none-elf-gcc …` | **no equivalent in Vivado** — use Vitis, or stay on the CLI |
| `capture_uart.py` | **no equivalent in Vivado** — Vitis Serial Terminal, GTKTerm, PuTTY, `screen`, `minicom` |
| `vivado -mode batch -source x.tcl` | Tools → Run Tcl Script…, or paste into the Tcl Console |

**The general rule:** anything Vivado does can be driven from the Tcl Console inside the GUI,
because the GUI is a Tcl front end. The GUI logs every action as Tcl — watch the Console while
you click, and you can lift the exact commands back into a script.

---

## Appendix C — troubleshooting by symptom

| Symptom | Likely cause | Check |
|---|---|---|
| No parts listed, `create_project` fails | Alveo license feature present | `grep INCREMENT ~/.Xilinx/*.lic`; strip `Vivado_Alveo_Package` |
| A7-Lite absent from Boards tab | expected — no board file exists | use the Parts tab |
| No JTAG target | power, cable in JTAG port, udev rules | `lsusb \| grep 0403:`, then Auto Connect |
| `Permission denied` on `/dev/ttyUSB0` | not in `dialout` | `groups`; `sudo usermod -aG dialout $USER`, then re-login |
| Latch inferred | `bhv/cv32e40x_sim_clock_gate.sv` compiled | remove all `bhv/` files; keep `rtl/cv32e40x_clock_gate.sv` |
| `[Synth 8-6841]` byte-write warning | RAM address wider than 12 bits | split RAM into byte lanes (§10.1) |
| Both LEDs dark | configuration or clock | check DONE, check `J19` constraint |
| LED1 blinks, LED2 dark | core not executing | BRAM init path, `BOOT_ADDR`, `$readmemh` file found? |
| LEDs fine, UART silent | wrong TX pin | verify `V2`; **do not drive `U2`** — contention |
| LEDs fine, UART garbage | baud divisor | `CLK_FREQ_HZ`/`BAUD_RATE`; 50 MHz/115200 = 434 |
| Counter restarts repeatedly | core trapping (`mtvec` = 0x0) | inspect `firmware.dis`; check stack/`.bss` bounds |
| Dropped lines in serial capture | JTAG traffic on the same USB bus | capture without programming concurrently (§9.3) |

---

## Summary

| Item | Value |
|---|---|
| Core | CV32E40X, unmodified, pinned at `d952cd6` |
| ISA | RV32IMC + Zicsr |
| Part | `xc7a35tfgg484-2L` (no board file — select by part) |
| Clock | 50 MHz on `J19` |
| Memory | 32 KB unified BRAM at `0x0`, four byte lanes |
| Peripherals | UART TX on `V2`, LEDs on `M18` / `N18` |
| Reset | on-chip power-on counter, no pin |
| CV-X-IF | instantiated, tied off, `X_EXT = 0` |
| Utilisation | 3733 LUTs (18%), 2304 FF, 8 RAMB36, 3 DSP |
| Timing | WNS +1.660 ns, WHS +0.084 ns → Fmax ≈ 54.5 MHz |
| Status | **verified on hardware** — 313 consecutive ticks from reset, zero gaps |
