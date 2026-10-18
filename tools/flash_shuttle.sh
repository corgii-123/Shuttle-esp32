#!/usr/bin/env bash
# Flash firmware/build/ over USB-Serial/JTAG and boot it.
#
#   tools/flash_shuttle.sh            # after: cd firmware && python build_shuttle.py
#
# Leaves the ROM loader with a watchdog reset instead of RTS: on macOS, the RTS reset
# (idf.py flash default) is followed by the port being re-opened, which drops the chip
# straight back into download mode ("waiting for download"). See tools/esp_console.py.
# First flash of a board running other firmware: hold BOOT (light-bulb key) while
# powering it on to force download mode.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
if ! command -v esptool >/dev/null 2>&1; then
  # ESP-IDF v6.1 accepts Python 3.10-3.13; if the default python3 is newer, put a 3.12 first
  # in PATH (here: ~/esp/pybin/python3 -> a uv/Homebrew python3.12, see docs/README.md §6.1).
  [[ -d "$HOME/esp/pybin" ]] && export PATH="$HOME/esp/pybin:$PATH"
  # shellcheck disable=SC1091
  . "${IDF_PATH:-$HOME/esp/esp-idf}/export.sh" >/dev/null
fi
PORT="${ESPPORT:-$(ls /dev/cu.usbmodem* 2>/dev/null | head -1)}"
[[ -n "$PORT" ]] || { echo "no /dev/cu.usbmodem* port"; exit 1; }
cd "$REPO/firmware/build"
esptool --chip esp32s3 --port "$PORT" --before default-reset --after watchdog-reset \
  write-flash @flash_args
