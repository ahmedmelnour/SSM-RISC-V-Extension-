## -----------------------------------------------------------------------------
## a7lite.xdc -- MicroPhase A7-Lite (XC7A35T-2FGG484)
##
## Every pin below is taken from the A7-LITE Reference Manual (Microphase FPGA_DOC
## V1.0), not from community examples:
##   Clocks : U10 CLK_50M  50 MHz -> J19
##   LED    : D6 LED1 -> M18, D5 LED2 -> N18
##   UART   : UART_TX -> V2 ("UART data output"), UART_RX -> U2 ("UART data input")
##
## On the UART naming: the manual's signal names are from the FPGA's point of view,
## so UART_TX/V2 is an FPGA *output* into the CH340's receiver. Note the manual also
## lists a flash part at "Position U2" -- that is a component designator, unrelated
## to FPGA ball U2. Do not conflate them.
##
## No reset pin is constrained: reset is generated on-chip by a power-on counter.
## -----------------------------------------------------------------------------

## ---- 50 MHz system clock ----
set_property -dict { PACKAGE_PIN J19  IOSTANDARD LVCMOS33 } [get_ports { clk_i }]
create_clock -add -name sys_clk -period 20.000 -waveform {0.000 10.000} [get_ports { clk_i }]

## ---- UART TX (FPGA -> CH340) ----
set_property -dict { PACKAGE_PIN V2   IOSTANDARD LVCMOS33 } [get_ports { uart_tx_o }]

## ---- User LEDs ----
set_property -dict { PACKAGE_PIN M18  IOSTANDARD LVCMOS33 } [get_ports { led1_o }]
set_property -dict { PACKAGE_PIN N18  IOSTANDARD LVCMOS33 } [get_ports { led2_o }]

## ---- Configuration ----
## 7-series requires CFGBVS/CONFIG_VOLTAGE or implementation fails DRC NSTD-1/UCIO-1.
set_property CFGBVS VCCO        [current_design]
set_property CONFIG_VOLTAGE 3.3 [current_design]
set_property BITSTREAM.GENERAL.COMPRESS TRUE [current_design]
