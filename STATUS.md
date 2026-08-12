# Status

Where the project actually is, updated as stages close. Detail on individual
problems lives in `doc/BRINGUP.md`.

## Done

**Stage 1 — board bring-up.** Blinky on the A7-Lite, LED and UART pins confirmed
against the manual. Kept in `blinky/` as the known-good reference.

**Stage 2 — core integration.** CV32E40X pinned at
`d952cd63` and instantiated with CV-X-IF present but tied off (`X_EXT = 0`), so
the port exists and is correctly typed for the accelerator later.

**Stage 3 — bare-metal firmware.** Linker script, `crt0.S`, freestanding build.
Verified in Spike: `sp` and `gp` load correctly, `.bss` is zeroed, the result
lands in memory. Byte-lane `.mem` images cross-checked against the full word.

**Stage 4 — SoC on hardware.** Synthesis and implementation clean, timing met
(WNS +1.660 ns, WHS +0.084 ns, zero failing endpoints of 7049). Memory infers as
4 × (8K × 8) true dual-port BRAM, 8 RAMB36, no `[Synth 8-6841]`.

## Open

- **Serial console.** No CH340 on the USB bus; the FT232H present is the Digilent
  JTAG programmer and cannot become a tty. Needs the board's own UART connector
  or a USB-TTL adapter on V2. `doc/BRINGUP.md` §12.
- **`doc/MEASUREMENT.md`** is a stub. Stage 5 work.

## Next

Stage 5 — cycle counting via `mcycle` and the HPM counters, and a baseline
measurement to compare the accelerator against. `-lgcc` is already in the
firmware link line, which Stage 5 needs for 64-bit division when printing
`mcycle`.
