#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# console.sh -- open the SoC's serial console.
#
#   ./scripts/console.sh          # 115200 8N1 on the first USB serial port
#   BAUD=9600 ./scripts/console.sh
#   ./scripts/console.sh /dev/ttyUSB1
#
# Exit picocom with Ctrl-A Ctrl-X.
#
# When there is no port, this says which of the causes it is rather than
# "FATAL: cannot open /dev/ttyUSB0", which is true but not useful.
# ---------------------------------------------------------------------------
set -euo pipefail

BAUD="${BAUD:-115200}"

# ---- pick a port ----------------------------------------------------------
port="${1:-}"

# Prefer the CH340 (1a86) by vendor id rather than taking the first tty. With two
# USB serial devices on the bus, ttyUSB0 is not reliably the UART, and picking the
# wrong one looks exactly like dead firmware. See doc/BRINGUP.md section 13.
if [ -z "$port" ]; then
    for p in /dev/ttyUSB* /dev/ttyACM*; do
        [ -e "$p" ] || continue        # unmatched globs stay literal
        vid=$(udevadm info -q property -n "$p" 2>/dev/null \
              | sed -n 's/^ID_VENDOR_ID=//p')
        if [ "$vid" = "1a86" ]; then
            port="$p"
            echo "[console] $p is the CH340 (vid 1a86)" >&2
            break
        fi
    done
fi

# Nothing identified as a CH340 -- fall back to the first port, but say so. A
# silent console after this is far more likely to be the wrong device than dead
# firmware.
if [ -z "$port" ]; then
    for p in /dev/ttyUSB* /dev/ttyACM*; do
        [ -e "$p" ] || continue
        port="$p"
        echo "[console] warning: no CH340 found, falling back to $p" >&2
        break
    done
fi

# ---- no port: work out why ------------------------------------------------
if [ -z "$port" ]; then
    echo "No USB serial port found (looked for /dev/ttyUSB* and /dev/ttyACM*)." >&2
    echo >&2

    if lsusb 2>/dev/null | grep -qi '1a86:'; then
        echo "  A CH340 IS on the bus but has no tty." >&2
        if dpkg -l 2>/dev/null | grep -q '^ii  brltty'; then
            echo "  brltty is installed and claims CH340 devices as braille displays." >&2
            echo "  That is almost certainly this. Fix:  sudo apt remove brltty" >&2
        else
            echo "  Check: dmesg | tail   and   lsmod | grep ch341" >&2
        fi
    else
        echo "  No CH340 (1a86:*) on the USB bus." >&2
        if lsusb 2>/dev/null | grep -qi '0403:6014'; then
            echo "  The FT232H that IS present is the Digilent JTAG programmer." >&2
            echo "  Single interface, driven over libusb by hw_server -- it will" >&2
            echo "  never appear as a tty. See doc/BRINGUP.md section 12." >&2
        fi
        echo >&2
        echo "  The UART needs its own connection: either the board's second USB" >&2
        echo "  port, or a USB-TTL adapter on pin V2 + ground." >&2
    fi

    echo >&2
    echo "  To watch a device enumerate while you plug it in:" >&2
    echo "    udevadm monitor --udev --subsystem-match=usb" >&2
    echo >&2
    echo "  LED1 and LED2 do not depend on any of this -- the core can be" >&2
    echo "  verified without a serial connection." >&2
    exit 1
fi

# ---- port exists but might not be usable ----------------------------------
if [ ! -r "$port" ] || [ ! -w "$port" ]; then
    echo "$port exists but is not readable/writable by $USER." >&2
    if ! id -nG | tr ' ' '\n' | grep -qx dialout; then
        echo "You are not in the 'dialout' group. Fix:" >&2
        echo "  sudo usermod -aG dialout $USER    # then log out and back in" >&2
    else
        echo "You are in 'dialout', so check: ls -l $port" >&2
    fi
    exit 1
fi

if ! command -v picocom >/dev/null 2>&1; then
    echo "picocom not installed:  sudo apt install picocom" >&2
    exit 1
fi

# ---- go -------------------------------------------------------------------
echo "[console] $port @ ${BAUD} 8N1   (Ctrl-A Ctrl-X to exit)"
exec picocom -b "$BAUD" "$port"
