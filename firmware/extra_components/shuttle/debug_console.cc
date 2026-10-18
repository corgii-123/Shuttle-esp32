#include "shuttle/debug_console.h"

#include <esp_log.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <sdkconfig.h>

#if CONFIG_SHUTTLE_DEBUG_CONSOLE && CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
#include <driver/usb_serial_jtag.h>
#include <driver/usb_serial_jtag_vfs.h>
#endif

namespace shuttle {

namespace {
const char* TAG = "DebugConsole";
}

bool StartDebugConsole(std::function<void(const std::string& line)> on_line) {
#if CONFIG_SHUTTLE_DEBUG_CONSOLE && CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
    if (!usb_serial_jtag_is_driver_installed()) {
        usb_serial_jtag_driver_config_t cfg = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
        cfg.tx_buffer_size = 4096;  // log bursts (screen dumps) without blocking the writer
        esp_err_t err = usb_serial_jtag_driver_install(&cfg);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "USB-Serial/JTAG driver install failed: %s", esp_err_to_name(err));
            return false;
        }
    }
    // The console now goes through the driver (interrupt-driven, drops output when no host
    // reads it) and its RX data reaches us instead of the polling VFS.
    usb_serial_jtag_vfs_use_driver();

    auto* handler = new std::function<void(const std::string&)>(std::move(on_line));
    BaseType_t ok = xTaskCreate(
        [](void* arg) {
            auto* cb = static_cast<std::function<void(const std::string&)>*>(arg);
            std::string line;
            uint8_t buf[64];
            while (true) {
                int n = usb_serial_jtag_read_bytes(buf, sizeof(buf), portMAX_DELAY);
                for (int i = 0; i < n; i++) {
                    char c = static_cast<char>(buf[i]);
                    if (c == '\r' || c == '\n') {
                        if (!line.empty()) {
                            ESP_LOGI(TAG, "> %s", line.c_str());
                            (*cb)(line);
                            line.clear();
                        }
                    } else if (line.size() < 160) {
                        line += c;
                    }
                }
            }
        },
        "debug_console", 4096, handler, 1, nullptr);
    if (ok != pdPASS) {
        ESP_LOGE(TAG, "cannot start the console task");
        delete handler;
        return false;
    }
    ESP_LOGW(TAG, "Debug console enabled (CONFIG_SHUTTLE_DEBUG_CONSOLE): type 'help'");
    return true;
#else
    (void)on_line;
    return false;
#endif
}

}  // namespace shuttle
