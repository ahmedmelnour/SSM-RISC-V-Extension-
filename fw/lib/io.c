/* ---------------------------------------------------------------------------
 * io.c -- UART output primitives.
 * --------------------------------------------------------------------------- */

#include "io.h"

void uart_putc(char c)
{
    while (UART_STATUS & UART_BUSY_BIT) {
        /* wait for the shift register to drain */
    }
    UART_TX = (unsigned int)(unsigned char)c;
}

void uart_puts(const char *s)
{
    while (*s) {
        if (*s == '\n') {
            uart_putc('\r');
        }
        uart_putc(*s++);
    }
}

void uart_put_u32(uint32_t v)
{
    char buf[10];
    int  i = 0;

    if (v == 0) { uart_putc('0'); return; }
    while (v > 0) { buf[i++] = (char)('0' + (v % 10u)); v /= 10u; }
    while (i > 0) { uart_putc(buf[--i]); }
}

void uart_put_u64(uint64_t v)
{
    char buf[20];
    int  i = 0;

    if (v == 0) { uart_putc('0'); return; }
    while (v > 0) { buf[i++] = (char)('0' + (uint32_t)(v % 10u)); v /= 10u; }
    while (i > 0) { uart_putc(buf[--i]); }
}

void uart_put_hex32(uint32_t v)
{
    static const char hex[] = "0123456789abcdef";
    uart_puts("0x");
    for (int shift = 28; shift >= 0; shift -= 4) {
        uart_putc(hex[(v >> shift) & 0xFu]);
    }
}

void uart_flush(void)
{
    while (UART_STATUS & UART_BUSY_BIT) {
    }
}

void led_set(int on)
{
    GPIO_OUT = on ? 1u : 0u;
}
