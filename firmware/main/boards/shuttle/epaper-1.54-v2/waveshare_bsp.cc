// Re-use the upstream Waveshare ESP32-S3-ePaper-1.54 drivers verbatim instead of copying
// them, so fixes from xiaozhi upstream flow in automatically. The included files resolve
// their own "config.h" relative to the waveshare directory.
#include "../../waveshare/esp32-s3-epaper-1.54/custom_lcd_display.cc"
#include "../../waveshare/esp32-s3-epaper-1.54/board_power_bsp.cc"
