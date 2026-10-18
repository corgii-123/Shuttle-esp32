#!/usr/bin/env python3
"""Serial console for the board's USB-Serial/JTAG port that does not drop it into the ROM loader.

Why: on macOS, opening the port raises DTR+RTS together, which the ESP32-S3 USB-Serial/JTAG
treats as "reset into download mode". `idf.py monitor` / plain pyserial therefore leave the
board sitting at "waiting for download". This tool opens the port with DTR+RTS already
asserted (no line change -> no reset), re-opens it when the USB device re-enumerates, and
can reboot the board via an RTC-watchdog reset (no DTR/RTS involved).

    python tools/esp_console.py                      # print the console
    python tools/esp_console.py --reboot             # reboot into the app first, then print
    python tools/esp_console.py --log serial.log -q  # append to a file only (background logger)
    python tools/esp_console.py --seconds 30 --until "Got IP"
    python tools/esp_console.py --log serial.log -q --fifo ~/esp/serial.in &
    echo "ptt 3000" > ~/esp/serial.in               # debug console command (see below)

--fifo forwards every line written to that named pipe to the board. Firmware built with
CONFIG_SHUTTLE_DEBUG_CONSOLE understands: ptt <ms> (hold the light-bulb key), tap, hold
(touch), pwr, dump (screen, decode with tools/screen_dump.py), stats <ms> (task CPU), reboot.

Needs pyserial + esptool (both in the ESP-IDF python env: . ~/esp/esp-idf/export.sh).
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
import threading
import time


def find_port() -> str | None:
    ports = sorted(glob.glob("/dev/cu.usbmodem*")) or sorted(glob.glob("/dev/ttyACM*"))
    return ports[0] if ports else None


def open_port(port: str):
    import serial

    s = serial.Serial()
    s.port, s.baudrate, s.timeout = port, 115200, 0.3
    s.dtr = True  # what the OS does on open anyway: no transition, no reset
    s.rts = True
    s.open()
    return s


def reboot(port: str) -> None:
    """ROM loader via esptool's USB-JTAG reset, then a watchdog reset into the app."""
    from esptool.cmds import detect_chip

    esp = detect_chip(port=port, baud=115200, connect_mode="default-reset", connect_attempts=3)
    esp.watchdog_reset()
    esp._port.close()


def forward_fifo(path: str, port: dict) -> None:
    """Write every line put into the named pipe `path` to the board (port["s"])."""
    if not os.path.exists(path):
        os.mkfifo(path)
    fd = os.open(path, os.O_RDWR)  # read+write: open() does not wait for a writer, no EOF
    pending = b""
    while True:
        pending += os.read(fd, 256)
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            s = port.get("s")
            if s is not None and line.strip():
                try:
                    s.write(line.strip() + b"\n")
                except OSError:
                    pass  # board resetting: the command is lost


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default=None, help="default: first /dev/cu.usbmodem*")
    ap.add_argument("--reboot", action="store_true", help="reboot the board into the app first")
    ap.add_argument("--seconds", type=float, default=0, help="stop after N seconds (0 = run forever)")
    ap.add_argument("--until", default="", help="stop once this text appears")
    ap.add_argument("--log", default="", help="append raw output to this file")
    ap.add_argument("-q", "--quiet", action="store_true", help="do not print (use with --log)")
    ap.add_argument("--fifo", default="", help="forward lines written to this named pipe to the board")
    args = ap.parse_args()

    port = args.port or find_port()
    if not port:
        print("no /dev/cu.usbmodem* port (board unplugged?)", file=sys.stderr)
        return 1
    if args.reboot:
        reboot(port)
    out = open(args.log, "ab", buffering=0) if args.log else None
    end = time.time() + args.seconds if args.seconds else None
    tail = b""
    s = None
    shared: dict = {"s": None}
    if args.fifo:
        threading.Thread(target=forward_fifo, args=(os.path.expanduser(args.fifo), shared), daemon=True).start()
    while end is None or time.time() < end:
        if s is None:
            p = args.port or find_port()
            try:
                s = open_port(p) if p else None
            except OSError:
                s = None
            shared["s"] = s
            if s is None:
                time.sleep(0.2)
                continue
        try:
            data = s.read(4096)
        except OSError:  # USB re-enumerated (chip reset): open it again
            shared["s"] = None
            s.close()
            s = None
            continue
        if not data:
            continue
        if out:
            out.write(data)
        if not args.quiet:
            sys.stdout.write(data.decode("utf-8", "replace"))
            sys.stdout.flush()
        tail = (tail + data)[-8192:]
        if args.until and args.until.encode() in tail:
            break
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
