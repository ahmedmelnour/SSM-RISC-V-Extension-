## clock
set_property -dict {PACKAGE_PIN J19 IOSTANDARD LVCMOS33} [get_ports clk_i]
create_clock -period 20.000 -name sys_clk [get_ports clk_i]

## LEDs
set_property -dict {PACKAGE_PIN M18 IOSTANDARD LVCMOS33} [get_ports led1_o]
set_property -dict {PACKAGE_PIN N18 IOSTANDARD LVCMOS33} [get_ports led2_o]