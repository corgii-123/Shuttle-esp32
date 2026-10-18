#include "shuttle_touch.h"

#include <esp_log.h>
#include <esp_timer.h>
#include <freertos/task.h>

#include <cstdlib>

#define TAG "ShuttleTouch"

namespace {
constexpr uint8_t kAddr = 0x38;
constexpr uint8_t kRegStatus = 0x02;   // TD_STATUS: number of touch points (bits 3:0)
constexpr uint8_t kRegChipId = 0xA3;
constexpr int64_t kLongPressUs = 600 * 1000;
constexpr int kTapSlopPx = 25;
}  // namespace

bool ShuttleTouch::Start(i2c_master_bus_handle_t bus, gpio_num_t int_pin, gpio_num_t rst_pin, Callback callback) {
    callback_ = std::move(callback);
    int_pin_ = int_pin;

    if (rst_pin != GPIO_NUM_NC) {
        gpio_config_t rst = {};
        rst.pin_bit_mask = 1ULL << rst_pin;
        rst.mode = GPIO_MODE_OUTPUT;
        gpio_config(&rst);
        gpio_set_level(rst_pin, 0);
        vTaskDelay(pdMS_TO_TICKS(10));
        gpio_set_level(rst_pin, 1);
        vTaskDelay(pdMS_TO_TICKS(150));  // controller boot time
    }

    i2c_device_config_t cfg = {};
    cfg.dev_addr_length = I2C_ADDR_BIT_LEN_7;
    cfg.device_address = kAddr;
    cfg.scl_speed_hz = 400000;
    if (i2c_master_bus_add_device(bus, &cfg, &dev_) != ESP_OK) {
        ESP_LOGW(TAG, "Cannot add touch device");
        return false;
    }
    uint8_t reg = kRegChipId, id = 0;
    if (i2c_master_transmit_receive(dev_, &reg, 1, &id, 1, 50) != ESP_OK) {
        ESP_LOGW(TAG, "No touch controller at 0x%02X", kAddr);
        i2c_master_bus_rm_device(dev_);
        dev_ = nullptr;
        return false;
    }
    ESP_LOGI(TAG, "FT6x36 touch controller, chip id 0x%02X (INT GPIO%d)", id, (int)int_pin);

    wake_ = xSemaphoreCreateBinary();
    if (int_pin_ != GPIO_NUM_NC) {
        gpio_config_t io = {};
        io.pin_bit_mask = 1ULL << int_pin_;
        io.mode = GPIO_MODE_INPUT;
        io.pull_up_en = GPIO_PULLUP_ENABLE;
        io.intr_type = GPIO_INTR_NEGEDGE;
        gpio_config(&io);
        esp_err_t err = gpio_install_isr_service(0);
        if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {  // already installed by others is fine
            ESP_LOGW(TAG, "gpio_install_isr_service: %s", esp_err_to_name(err));
        }
        gpio_isr_handler_add(int_pin_, OnInterrupt, this);
    }
    xTaskCreate(TaskEntry, "shuttle_touch", 3072, this, 4, nullptr);
    return true;
}

void IRAM_ATTR ShuttleTouch::OnInterrupt(void* arg) {
    auto* self = static_cast<ShuttleTouch*>(arg);
    BaseType_t woken = pdFALSE;
    xSemaphoreGiveFromISR(self->wake_, &woken);
    portYIELD_FROM_ISR(woken);
}

bool ShuttleTouch::ReadPoint(int& x, int& y) {
    uint8_t reg = kRegStatus, buf[5] = {};
    if (i2c_master_transmit_receive(dev_, &reg, 1, buf, sizeof(buf), 50) != ESP_OK) {
        return false;
    }
    if ((buf[0] & 0x0F) == 0) {
        return false;
    }
    x = ((buf[1] & 0x0F) << 8) | buf[2];
    y = ((buf[3] & 0x0F) << 8) | buf[4];
    return true;
}

void ShuttleTouch::TaskEntry(void* arg) { static_cast<ShuttleTouch*>(arg)->Task(); }

void ShuttleTouch::Task() {
    while (true) {
        // Wake on the INT edge; also poll every 500 ms in case an edge was missed.
        xSemaphoreTake(wake_, pdMS_TO_TICKS(int_pin_ != GPIO_NUM_NC ? 500 : 50));
        int x0 = 0, y0 = 0;
        if (!ReadPoint(x0, y0)) {
            continue;
        }
        int64_t t0 = esp_timer_get_time();
        int x = x0, y = y0;
        bool long_sent = false;
        bool moved = false;
        while (true) {
            vTaskDelay(pdMS_TO_TICKS(30));
            int nx, ny;
            if (!ReadPoint(nx, ny)) {
                break;
            }
            x = nx;
            y = ny;
            moved = moved || std::abs(x - x0) > kTapSlopPx || std::abs(y - y0) > kTapSlopPx;
            if (!long_sent && !moved && esp_timer_get_time() - t0 >= kLongPressUs) {
                long_sent = true;
                ESP_LOGI(TAG, "Long press at (%d,%d)", x0, y0);
                callback_(Gesture::kLongPress, x0, y0);
            }
        }
        int ms = (int)((esp_timer_get_time() - t0) / 1000);
        if (!long_sent && !moved) {
            ESP_LOGI(TAG, "Tap at (%d,%d) %d ms", x0, y0, ms);
            callback_(Gesture::kTap, x0, y0);
        } else if (moved) {
            ESP_LOGI(TAG, "Swipe (%d,%d) -> (%d,%d) %d ms (ignored)", x0, y0, x, y, ms);
        }
        xSemaphoreTake(wake_, 0);  // drop edges that arrived while polling
    }
}
