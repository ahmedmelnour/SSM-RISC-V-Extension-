/* ---------------------------------------------------------------------------
 * crt0.S -- minimal startup for CV32E40X.
 *
 * mtvec is tied to BOOT_ADDR (0x0) in the SoC, so any trap re-enters _start.
 * That is deliberate for bring-up: main() prints a counter that restarts from
 * zero, so a trap loop is visible on the terminal as the count resetting rather
 * than as a silent hang.
 * --------------------------------------------------------------------------- */

    .section .text.init, "ax"
    .global _start
    .type _start, @function

_start:
    /* No interrupts during startup. */
    csrw  mie, zero
    csrw  mstatus, zero

    /* Stack and global pointer. gp must be set with relaxation disabled,
       otherwise the assembler rewrites la gp,__global_pointer$ relative to gp
       itself (which is not yet valid). */
    la    sp, _stack_top
.option push
.option norelax
    la    gp, __global_pointer$
.option pop

    /* Zero .bss */
    la    a0, _sbss
    la    a1, _ebss
1:  bgeu  a0, a1, 2f
    sw    zero, 0(a0)
    addi  a0, a0, 4
    j     1b

2:  /* Clear the integer registers we care about, then enter main. */
    li    a0, 0
    li    a1, 0
    call  main

3:  /* main() should not return; if it does, spin. */
    j     3b

    .size _start, .-_start
