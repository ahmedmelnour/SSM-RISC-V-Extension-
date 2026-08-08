/* ---------------------------------------------------------------------------
 * io.h -- UART output and the GPIO LED, for the SoC in rtl/a7lite_soc_top.sv.
 * --------------------------------------------------------------------------- */

#ifndef IO_H
#define IO_H

#include <stdint.h>

#define UART_TX     (*(volatile unsigned int *)0x10000000u)
#define UART_STATUS (*(volatile unsigned int *)0x10000004u)
#define GPIO_OUT    (*(volatile unsigned int *)0x10000008u)

#define UART_BUSY_BIT 0x1u

void uart_putc(char c);
void uart_puts(const char *s);      /* '\n' is expanded to "\r\n" */
void uart_put_u32(uint32_t v);
void uart_put_u64(uint64_t v);
void uart_put_hex32(uint32_t v);

/* Block until the transmitter has drained. Use before halting or resetting,
 * otherwise the tail of the last line is lost. */
void uart_flush(void);

void led_set(int on);

#endif /* IO_H */
