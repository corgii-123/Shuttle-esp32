// FT6336 touch panel of the ESP32-S3-Touch-ePaper-1.54 (I2C 0x38 on the codec bus,
// INT GPIO21, RST GPIO7 per Waveshare's 12_FT6336_Test example).
//
// Interrupt-driven: the INT falling edge wakes a small task, which then polls the
// controller every 30 ms until the finger is lifted and reports one gesture:
//   kTap       press < 600 ms, finger moved < 25 px
//   kLongPress press >= 600 ms (reported once, while still pressed)
#pragma once

#include <driver/gpio.h>
#include <driver/i2c_master.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

#include <functional>

class ShuttleTouch {
public:
    enum class Gesture { kTap, kLongPress };
    using Callback = std::function<void(Gesture gesture, int x, int y)>;

    // Returns false (and logs) when no controller answers; touch is then simply unused.
    bool Start(i2c_master_bus_handle_t bus, gpio_num_t int_pin, gpio_num_t rst_pin, Callback callback);

private:
    static void OnInterrupt(void* arg);
    static void TaskEntry(void* arg);
    void Task();
    bool ReadPoint(int& x, int& y);  // true while touched

    i2c_master_dev_handle_t dev_ = nullptr;
    gpio_num_t int_pin_ = GPIO_NUM_NC;
    SemaphoreHandle_t wake_ = nullptr;
    Callback callback_;
};
