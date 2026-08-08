# -----------------------------------------------------------------------------
# program.tcl -- configure the A7-Lite over JTAG (volatile, RAM configuration).
#
#   vivado -mode batch -source scripts/program.tcl
#
# Reports DONE afterwards. Note that DONE=1 proves configuration succeeded; it
# says nothing about whether the pin assignments are correct.
# -----------------------------------------------------------------------------

set root [file normalize [file join [file dirname [info script]] ..]]
set bit  $root/build/a7lite_soc.bit

if {![file exists $bit]} {
  error "bitstream not found: $bit"
}

open_hw_manager
connect_hw_server -quiet
set targets [get_hw_targets -quiet]
if {[llength $targets] == 0} {
  error "no JTAG target found -- is the board powered and the USB cable in the JTAG port?"
}

open_hw_target [lindex $targets 0]
set dev [lindex [get_hw_devices -quiet] 0]
current_hw_device $dev
refresh_hw_device -quiet $dev
puts "INFO: device = $dev  IDCODE = [get_property IDCODE $dev]"

set_property PROGRAM.FILE $bit $dev
program_hw_devices $dev
refresh_hw_device -quiet $dev

set done [get_property REGISTER.CONFIG_STATUS.BIT14_DONE_PIN $dev]
puts "INFO: DONE = $done"
close_hw_target
if {$done != 1} {
  error "configuration failed: DONE = $done"
}
puts "INFO: configuration OK"
