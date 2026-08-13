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
import glob
import os
import sys
import termios
import time

# The board presents TWO USB serial devices: an FT232H (0403:6014) for JTAG and
# a CH340 (1a86:7523) for the UART. Which one gets ttyUSB0 depends on enumeration
# order, so it changes between boots -- and between re-plugs within one session.
# Listening on the wrong one reports SILENT, which looks exactly like dead
# firmware, a wrong baud or a bad pin constraint. Resolve by vendor ID instead.
# See doc/BRINGUP.md section 13.
CH340_VID = "1a86"
FT232H_VID = "0403"

BAUD_CONST = {
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
}


def usb_vid(tty_path):
    """Vendor ID of the USB device behind a tty, or None. Walks up sysfs from
    /sys/class/tty/<name>/device until it finds an idVendor."""
    name = os.path.basename(tty_path)
    path = os.path.realpath("/sys/class/tty/%s/device" % name)
    for _ in range(8):
        candidate = os.path.join(path, "idVendor")
        if os.path.exists(candidate):
            try:
                with open(candidate) as fh:
                    return fh.read().strip().lower()
            except OSError:
                return None
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return None


def resolve_port(requested):
    """Return the CH340's tty. An explicit --port is honoured as-is."""
    if requested and requested != "auto":
        return requested

    candidates = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    if not candidates:
        raise SystemExit(
            "no USB serial port found (looked for /dev/ttyUSB* and /dev/ttyACM*).\n"
            "  Is the board's UART cable connected? The FT232H JTAG programmer is\n"
            "  driven over libusb and never appears as a tty -- see doc/BRINGUP.md 12."
        )

    for path in candidates:
        if usb_vid(path) == CH340_VID:
            return path

    detail = ", ".join("%s (vid %s)" % (p, usb_vid(p) or "?") for p in candidates)
    raise SystemExit(
        "no CH340 (%s) among the USB serial ports: %s\n"
        "  The FT232H (%s) is the JTAG programmer, not the UART. Pass --port\n"
        "  explicitly if your UART is on some other adapter."
        % (CH340_VID, detail, FT232H_VID)
    )


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
    ap.add_argument("--port", default="auto",
                    help="tty to read, or 'auto' to find the CH340 by vendor id")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--expect", default="CV32E40X")
    args = ap.parse_args()

    args.port = resolve_port(args.port)
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
