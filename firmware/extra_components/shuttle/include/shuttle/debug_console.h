// Development console on the USB-Serial/JTAG port (CONFIG_SHUTTLE_DEBUG_CONSOLE).
//
// Reads command lines sent by the computer the board is plugged into
// (`tools/esp_console.py --fifo`, then `echo "ptt 3000" > fifo`), so tests can press keys
// and inspect the device without touching it. Off by default: anyone with the USB cable
// could, e.g., start the microphone.
#pragma once

#include <functional>
#include <string>

namespace shuttle {

// Starts a low-priority task that calls on_line() for every non-empty line received.
// Does nothing (returns false) unless CONFIG_SHUTTLE_DEBUG_CONSOLE is set and the console
// is the USB-Serial/JTAG port. on_line runs in that task: keep it short (Schedule work).
bool StartDebugConsole(std::function<void(const std::string& line)> on_line);

}  // namespace shuttle
