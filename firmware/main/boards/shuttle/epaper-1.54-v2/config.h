#ifndef _SHUTTLE_BOARD_CONFIG_H_
#define _SHUTTLE_BOARD_CONFIG_H_

// Hardware is the Waveshare ESP32-S3-ePaper-1.54 (V2: 8MB flash + 8MB octal PSRAM).
// Pins, codec (ES8311) and e-paper wiring are identical for V1 and V2 (verified against
// waveshareteam/ESP32-S3-ePaper-1.54 V1/V2 examples), so the upstream board config is the
// single source of truth. Do not copy pin numbers here.
#include "../../waveshare/esp32-s3-epaper-1.54/config.h"

// Green user LED blinked by the upstream BoardPowerBsp (GPIO3, also a strapping pin).
#define SHUTTLE_LED_GPIO GPIO_NUM_3

// FT6336 touch panel of the "ESP32-S3-Touch-ePaper-1.54" variant (I2C 0x38 on the codec
// bus). Pins from waveshareteam/ESP32-S3-ePaper-1.54 02_Example/Arduino/12_FT6336_Test.
// Boards without touch simply do not answer at 0x38 and touch stays off.
#define SHUTTLE_TOUCH_INT_GPIO GPIO_NUM_21
#define SHUTTLE_TOUCH_RST_GPIO GPIO_NUM_7

#endif  // _SHUTTLE_BOARD_CONFIG_H_
