# Shuttle board: Waveshare ESP32-S3-(Touch-)ePaper-1.54 **V2**

Same hardware as upstream `waveshare/esp32-s3-epaper-1.54` (variant `esp32-s3-epaper-1.54-v2`):
ESP32-S3-PICO-1-N8R8 (8 MB flash, 8 MB octal PSRAM), ES8311 codec + mic + PA, 200×200 SSD1681-class
e-paper, BOOT (GPIO0) and PWR (GPIO18) keys, battery ADC on GPIO4 (ADC1_CH3, 1:2 divider).
Pins come from `../../waveshare/esp32-s3-epaper-1.54/config.h` (not copied).

| Key / touch | Action |
|---|---|
| BOOT (light-bulb key) hold | push-to-talk: records from the press while held (+`CONFIG_SHUTTLE_PTT_TAIL_MS`), sends on release |
| BOOT click during boot | open Wi-Fi portal (upstream behaviour) |
| PWR click | switch to the best available Wi-Fi now (e.g. hotspot → home) |
| PWR ×2 | open Wi-Fi portal (AP `Shuttle-XXXX`, http://192.168.4.1) |
| PWR long press | power off (unchanged from upstream) |
| tap the screen | while a task runs: cancel it; while idle: device details for 10 s |
| long press on the screen | ghost-clearing full refresh |

The touch panel (FT6336, I2C 0x38, INT GPIO21, RST GPIO7) exists on the *ESP32-S3-Touch-ePaper-1.54*;
boards without it are detected at boot (I2C scan) and simply have no touch. The portal is on
PWR ×2 rather than BOOT ×3: a bouncy key produced triple clicks (→ portal) while talking.

Build: `python build_shuttle.py` (see `firmware/build_shuttle.py`, `docs/README.md`).
