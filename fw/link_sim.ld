/* ---------------------------------------------------------------------------
 * link_sim.ld -- link.ld with RAM relocated to 0x8000_0000 for Spike.
 *
 * Hand-maintained, despite once being labelled "generated" -- nothing generates
 * it. That is a drift hazard: this is the FOURTH place the memory size is written
 * down (doc/BRINGUP.md §10.1 lists the other three) and it silently stayed at 32K
 * through the widening to 128K. LENGTH must track MEM_WORDS in
 * rtl/a7lite_soc_top.sv, MEM_WORDS in fw/build.sh, and LENGTH in fw/link.ld.
 *
 * Simulation only: Spike boots at 0x1000 and maps a boot ROM there, so a RAM
 * region based at 0x0 would collide with it. Not the SoC memory map.
 * --------------------------------------------------------------------------- */

OUTPUT_ARCH( "riscv" )
ENTRY( _start )

MEMORY
{
  RAM (rwx) : ORIGIN = 0x80000000, LENGTH = 128K
}

SECTIONS
{
  /* _start must be the very first thing at address 0x0: that is BOOT_ADDR. */
  .text : {
    *(.text.init)
    *(.text .text.*)
    *(.rodata .rodata.*)
    . = ALIGN(4);
  } > RAM

  .data : {
    . = ALIGN(4);
    __global_pointer$ = . + 0x800;
    *(.sdata .sdata.*)
    *(.data .data.*)
    . = ALIGN(4);
  } > RAM

  .bss (NOLOAD) : {
    . = ALIGN(4);
    _sbss = .;
    *(.sbss .sbss.*)
    *(.bss .bss.*)
    *(COMMON)
    . = ALIGN(4);
    _ebss = .;
  } > RAM

  /* Stack grows down from the top of RAM. */
  _stack_top = ORIGIN(RAM) + LENGTH(RAM);

  /DISCARD/ : { *(.comment) *(.note*) *(.eh_frame*) }
}
