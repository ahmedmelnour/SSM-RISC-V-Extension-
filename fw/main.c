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
 * --------------------------------------------------------------------------- */

#define UART_TX      (*(volatile unsigned int *)0x10000000u)
#define UART_STATUS  (*(volatile unsigned int *)0x10000004u)
#define GPIO_OUT     (*(volatile unsigned int *)0x10000008u)

#define UART_BUSY_BIT 0x1u

static void uart_putc(char c)
{
    while (UART_STATUS & UART_BUSY_BIT) {
        /* wait for the shift register to drain */
    }
    UART_TX = (unsigned int)(unsigned char)c;
}

static void uart_puts(const char *s)
{
    while (*s) {
        if (*s == '\n') {
            uart_putc('\r');
        }
        uart_putc(*s++);
    }
}

static void uart_putdec(unsigned int v)
{
    char buf[11];
    int  i = 0;

    if (v == 0) {
        uart_putc('0');
        return;
    }
    while (v > 0) {
        buf[i++] = (char)('0' + (v % 10u));
        v /= 10u;
    }
    while (i > 0) {
        uart_putc(buf[--i]);
    }
}

static void delay(unsigned int n)
{
    /* volatile so -Os cannot delete the loop */
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
    uart_puts(" RV32IMC @ 50 MHz, 32KB BRAM\n");
    uart_puts("=====================================\n");

    for (;;) {
        led ^= 1u;
        GPIO_OUT = led;

        uart_puts("tick ");
        uart_putdec(count);
        uart_puts("\n");

        count++;
        delay(400000u);   /* measured ~110 ms/tick on hardware */
    }

    return 0;
}
