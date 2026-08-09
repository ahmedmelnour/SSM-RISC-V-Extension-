/* ---------------------------------------------------------------------------
 * main.c -- CV32E40X bring-up firmware for the A7-Lite.
 *
 * Prints a banner and then an incrementing counter, toggling LED2 on every
 * iteration. Between them these cover the three failure modes independently:
 *
 *   LED2 toggling  -> the core fetches, executes and completes stores.
 *   banner text    -> the UART pin and baud divisor are right.
 *   counter resets -> the core is trapping and restarting (mtvec == 0x0),
 *                     rather than the count simply continuing upward.
 *
 * For measurement work use bench.c instead; this program stays deliberately
 * minimal so it remains a trustworthy "is the board alive?" test.
 * --------------------------------------------------------------------------- */

#include "lib/io.h"

static void delay(unsigned int n)
{
    /* volatile so the optimiser cannot delete the loop */
    for (volatile unsigned int i = 0; i < n; i++) {
    }
}

int main(void)
{
    unsigned int count = 0;
    unsigned int led   = 0;

    uart_puts("\n");
    uart_puts("=====================================\n");
    uart_puts(" CV32E40X alive on MicroPhase A7-Lite\n");
    uart_puts(" RV32IMC @ 50 MHz, 128KB BRAM\n");
    uart_puts("=====================================\n");

    for (;;) {
        led ^= 1u;
        led_set((int)led);

        uart_puts("tick ");
        uart_put_u32(count);
        uart_puts("\n");

        count++;
        delay(400000u);   /* measured ~110 ms/tick on hardware */
    }

    return 0;
}
