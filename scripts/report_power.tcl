# -----------------------------------------------------------------------------
# report_power.tcl -- FPGA power estimation from the routed checkpoint.
#
#   vivado -mode batch -source scripts/report_power.tcl
#   vivado -mode batch -source scripts/report_power.tcl -tclargs <name> <saif>
#
# Two modes, and the difference matters for what you may claim:
#
#   VECTORLESS (default)
#     No switching activity data. Vivado assumes a default toggle rate (12.5%)
#     on unconstrained nets. Confidence is reported as "Low". Usable for
#     *relative* comparison between two designs built the same way; not usable
#     as an absolute power number in a report.
#
#   SAIF-DRIVEN (pass a .saif)
#     Real per-net switching activity from a post-synthesis or post-implementation
#     simulation of the actual kernel. This is what a PPA claim should rest on.
#     Generate one with write_saif in xsim over a testbench running the kernel.
#
# Either way this is a *model*, not a measurement. For measured power see
# doc/MEASUREMENT.md -- the A7-Lite has no on-board current-sense shunt, so
# board-level measurement means an inline ammeter on the 5 V USB feed.
# -----------------------------------------------------------------------------

set root [file normalize [file join [file dirname [info script]] ..]]
set dcp  $root/build/post_route.dcp
set out  $root/build

set name [expr {[llength $argv] > 0 ? [lindex $argv 0] : "vectorless"}]
set saif [expr {[llength $argv] > 1 ? [lindex $argv 1] : ""}]

if {![file exists $dcp]} {
  error "no routed checkpoint at $dcp -- run scripts/build.tcl first"
}

open_checkpoint $dcp

if {$saif ne ""} {
  if {![file exists $saif]} { error "SAIF not found: $saif" }
  puts "INFO: reading switching activity from $saif"
  read_saif $saif
} else {
  puts "INFO: vectorless estimation -- no SAIF supplied."
  puts "INFO: treat the result as relative, not absolute."
  set_switching_activity -default_toggle_rate 12.5 -default_static_probability 0.5

  # Without this, the vectorless engine assumes the high-fanout reset toggles
  # like any other net and is asserted ~50% of the time, which inflates dynamic
  # power and triggers [Power 33-332]. This design's reset is a power-on pulse:
  # asserted for 65536 cycles once, then never again.
  #
  # Find the reset structurally, by the pins it drives, NOT by name: synthesis
  # renames it (here `rst_n` became `u_core_n_57`), so any name pattern silently
  # matches nothing and the correction is quietly skipped.
  #
  # These pins (FDRE.R, FDCE.CLR, FDPE.PRE, FDSE.S) are all ACTIVE HIGH, so the
  # net driving them is the inverse of rst_n -- its static probability is ~0,
  # not ~1.
  set rst_pins [get_pins -quiet -hier -filter \
                {REF_PIN_NAME == R || REF_PIN_NAME == CLR || \
                 REF_PIN_NAME == PRE || REF_PIN_NAME == S}]
  set rst_nets [filter [get_nets -quiet -of_objects $rst_pins] {NAME !~ *const*}]
  if {[llength $rst_nets] > 0} {
    set_switching_activity -static_probability 0.0 -toggle_rate 0.0 $rst_nets
    puts "INFO: pinned [llength $rst_nets] reset net(s) to deasserted:"
    foreach n $rst_nets { puts "        $n (fanout [get_property FLAT_PIN_COUNT $n])" }
  } else {
    puts "WARNING: no reset nets identified -- [Power 33-332] may skew dynamic power"
  }
}

set rpt $out/power_$name.rpt
report_power -file $rpt
report_power -hierarchical_depth 3 -file $out/power_${name}_hier.rpt

# ---------------------------------------------------------------------------
# Pull the headline numbers back out so the flow can be scripted.
#
# Note: there is no TOTAL_POWER property on the design object -- report_power
# does not publish its results that way. The report file is the only source, so
# parse it.
# ---------------------------------------------------------------------------
proc power_field {file pattern} {
  if {![file exists $file]} { return "NA" }
  set fh [open $file r]
  set data [read $fh]
  close $fh
  foreach line [split $data "\n"] {
    if {[regexp $pattern $line -> val]} { return [string trim $val] }
  }
  return "NA"
}

set total   [power_field $rpt {Total On-Chip Power \(W\)\s*\|\s*([0-9.]+)}]
set dynamic [power_field $rpt {Dynamic \(W\)\s*\|\s*([0-9.]+)}]
set static  [power_field $rpt {Device Static \(W\)\s*\|\s*([0-9.]+)}]
set conf    [power_field $rpt {Confidence Level\s*\|\s*(\w+)}]
set tjunc   [power_field $rpt {Junction Temperature \(C\)\s*\|\s*([0-9.]+)}]

puts ""
puts "==================================================================="
puts " POWER ($name)"
puts "   total    = $total W"
puts "   dynamic  = $dynamic W"
puts "   static   = $static W"
puts "   Tj       = $tjunc C"
puts "   confidence = $conf"
puts "   report   = $rpt"
puts "==================================================================="

set fh [open $out/power_$name.csv w]
puts $fh "metric,value,unit"
puts $fh "total_power,$total,W"
puts $fh "dynamic_power,$dynamic,W"
puts $fh "static_power,$static,W"
puts $fh "junction_temp,$tjunc,C"
puts $fh "confidence,$conf,"
puts $fh "source,[expr {$saif ne "" ? "saif" : "vectorless"}],"
close $fh
puts "INFO: machine-readable summary -> $out/power_$name.csv"
