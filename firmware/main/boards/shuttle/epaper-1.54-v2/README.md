# Shuttle board: Waveshare ESP32-S3-ePaper-1.54 **V2**

Same hardware as upstream `waveshare/esp32-s3-epaper-1.54` (variant `esp32-s3-epaper-1.54-v2`):
ESP32-S3-PICO-1-N8R8 (8 MB flash, 8 MB octal PSRAM), ES8311 codec + mic + PA, 200×200 SSD1681-class
e-paper, BOOT (GPIO0) and PWR (GPIO18) keys, battery ADC on GPIO4 (ADC1_CH3, 1:2 divider).
Pins come from `../../waveshare/esp32-s3-epaper-1.54/config.h` (not copied).

| Key | Action |
|---|---|
| BOOT hold | push-to-talk: record while held, send on release |
| BOOT click during boot | open Wi-Fi portal (upstream behaviour) |
| BOOT ×3 | open Wi-Fi portal (AP `Shuttle-XXXX`, http://192.168.4.1) |
| PWR long press | power off (unchanged from upstream) |

Build: `python build_shuttle.py` (see `firmware/build_shuttle.py`, `docs/README.md`).
