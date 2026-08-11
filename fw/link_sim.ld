/* ---------------------------------------------------------------------------
 * link.ld -- single 32 KB RAM at 0x8000_0000, matching the SoC memory map.
 * LENGTH must track MEM_WORDS in rtl/a7lite_soc_top.sv and fw/build.sh.
 * Code, data and stack all live in the same BRAM. The core boots at 0x0.
 * --------------------------------------------------------------------------- */

OUTPUT_ARCH( "riscv" )
ENTRY( _start )

MEMORY
{
  RAM (rwx) : ORIGIN = 0x80000000, LENGTH = 32K
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
