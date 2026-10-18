/* Host-side LVGL config for the Shuttle UI simulator (mirrors the device: RGB565, sw renderer). */
#ifndef LV_CONF_H
#define LV_CONF_H
#define LV_COLOR_DEPTH 16
#define LV_USE_STDLIB_MALLOC LV_STDLIB_CLIB
#define LV_USE_STDLIB_STRING LV_STDLIB_CLIB
#define LV_USE_STDLIB_SPRINTF LV_STDLIB_CLIB
#define LV_USE_OS LV_OS_NONE
#define LV_USE_LOG 1
#define LV_LOG_LEVEL LV_LOG_LEVEL_WARN
#define LV_LOG_PRINTF 1
#define LV_FONT_MONTSERRAT_14 1
#define LV_USE_DRAW_SW 1
#define LV_DRAW_SW_COMPLEX 1
#define LV_USE_IMAGE 1
#define LV_USE_LABEL 1
#define LV_USE_LINE 1
/* Same font options as firmware/sdkconfig.defaults (needed by the xiaozhi fonts) */
#define LV_FONT_FMT_TXT_LARGE 1
#define LV_USE_FONT_COMPRESSED 0
#define LV_USE_FONT_PLACEHOLDER 0
#define LV_USE_ASSERT_NULL 1
#endif
