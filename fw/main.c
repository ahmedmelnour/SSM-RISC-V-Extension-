/* ---------------------------------------------------------------------------
 * main.c -- Stage 3 sanity program. No peripherals exist yet, so "output"
 * means a value in memory that the simulator can be asked to print.
 * --------------------------------------------------------------------------- */

volatile unsigned int result;    /* volatile: do not optimise me away */

int main(void) {
    unsigned int sum = 0;
    for (unsigned int i = 1; i <= 100; i++) sum += i;
    result = sum;                /* 5050 = 0x13BA */
    for (;;) { }
}
