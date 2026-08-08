#!/usr/bin/env python3
"""
capture_uart.py -- read the CH340 back-channel and report what arrived.

Used to decide, on hardware, whether the UART TX pin assignment is correct.
Distinguishes three outcomes, because they need different fixes:

  OK       readable ASCII containing the expected banner  -> pin and baud correct
  GARBAGE  bytes arrived but are not valid ASCII          -> right pin, wrong baud
  SILENT   nothing at all                                 -> wrong pin

Deliberately dependency-free (termios, not pyserial) so it needs no install.
"""

import argparse
import os
import sys
import termios
import time

BAUD_CONST = {
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
}


def open_port(path, baud):
    fd = os.open(path, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    attrs = termios.tcgetattr(fd)
    iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs

    # 8N1, raw, no flow control, ignore modem control lines.
    cflag = termios.CS8 | termios.CREAD | termios.CLOCAL
    iflag = termios.IGNPAR
    oflag = 0
    lflag = 0
    cc = list(cc)
    cc[termios.VMIN] = 0
    cc[termios.VTIME] = 0

    speed = BAUD_CONST[baud]
    termios.tcsetattr(
        fd, termios.TCSANOW, [iflag, oflag, cflag, lflag, speed, speed, cc]
    )
    termios.tcflush(fd, termios.TCIFLUSH)
    return fd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--expect", default="CV32E40X")
    args = ap.parse_args()

    fd = open_port(args.port, args.baud)
    buf = bytearray()
    deadline = time.time() + args.seconds

    while time.time() < deadline:
        try:
            chunk = os.read(fd, 4096)
        except BlockingIOError:
            chunk = b""
        if chunk:
            buf += chunk
        else:
            time.sleep(0.02)
    os.close(fd)

    print("=" * 60)
    print("port=%s baud=%d window=%.1fs bytes=%d" % (args.port, args.baud, args.seconds, len(buf)))
    print("=" * 60)

    if not buf:
        print("RESULT: SILENT -- no bytes received")
        return 2

    text = buf.decode("ascii", errors="replace")
    print(text)
    print("=" * 60)
    print("raw hex (first 64): %s" % buf[:64].hex(" "))

    printable = sum(1 for b in buf if 32 <= b < 127 or b in (10, 13))
    ratio = printable / len(buf)

    if args.expect in text:
        print("RESULT: OK -- found %r (printable %.0f%%)" % (args.expect, ratio * 100))
        return 0
    if ratio > 0.9:
        print("RESULT: ASCII but banner %r not seen (printable %.0f%%)" % (args.expect, ratio * 100))
        return 1
    print("RESULT: GARBAGE -- %.0f%% printable, suspect baud mismatch" % (ratio * 100))
    return 3


if __name__ == "__main__":
    sys.exit(main())
