#ifndef _SHUTTLE_BOARD_CONFIG_H_
#define _SHUTTLE_BOARD_CONFIG_H_

// Hardware is the Waveshare ESP32-S3-ePaper-1.54 (V2: 8MB flash + 8MB octal PSRAM).
// Pins, codec (ES8311) and e-paper wiring are identical for V1 and V2 (verified against
// waveshareteam/ESP32-S3-ePaper-1.54 V1/V2 examples), so the upstream board config is the
// single source of truth. Do not copy pin numbers here.
#include "../../waveshare/esp32-s3-epaper-1.54/config.h"

// Green user LED blinked by the upstream BoardPowerBsp (GPIO3, also a strapping pin).
#define SHUTTLE_LED_GPIO GPIO_NUM_3

#endif  // _SHUTTLE_BOARD_CONFIG_H_
