# Upstream: xiaozhi-esp32

`firmware/` is a vendored copy of **[78/xiaozhi-esp32](https://github.com/78/xiaozhi-esp32)**
(without its `.git`, `.github`, `docker`), plus Shuttle additions.

| | |
|---|---|
| Upstream commit | `d7a1c05308fb6e116ffb44b9e2c6e67b3366302a` (2026-10-15, PROJECT_VER 2.5.3) |
| ESP-IDF | v6.1 (upstream minimum v6.0.1) |
| Board used | upstream `waveshare/esp32-s3-epaper-1.54` (V2 variant) — pins/drivers reused as-is |

## What Shuttle adds (new files only)

```
firmware/extra_components/shuttle/          ESP-IDF component (pure IDF, no xiaozhi deps)
  Kconfig                                   menuconfig: server, token, Wi-Fi list, bridge tuning
  shuttle_config.cc / include/shuttle/*.h   config access, NVS provisioning, last phone per SSID
  phone_scanner.cc                          finds phones (adbd on :5555) on the local /24
  adb_bridge.cc                             TCP(phone:5555) <-> WebSocket(/ws/adb) byte bridge
firmware/main/boards/shuttle/epaper-1.54-v2/
  config.h / config.json / README.md        board identity; includes the waveshare config.h
  waveshare_bsp.cc                          #includes the upstream waveshare driver .cc files
  shuttle_board.cc                          board class (PTT, battery, bridge, JSON hook)
  shuttle_display.{h,cc}                    e-paper UI
  shuttle_wifi.{h,cc}                       priority Wi-Fi on top of esp-wifi-connect
firmware/build_shuttle.py                   build wrapper (private settings -> config.local.json)
firmware/shuttle.local.conf.example
```

## Patches to upstream files (keep these minimal)

All marked with `SHUTTLE:` comments. `upstream.patch` holds the exact diff.

| File | Change |
|---|---|
| `CMakeLists.txt` | `EXTRA_COMPONENT_DIRS += extra_components` (upstream git-ignores `components/`) |
| `main/Kconfig.projbuild` | board choice `BOARD_TYPE_SHUTTLE_EPAPER_1_54_V2` (alphabetical, upstream test requires it) |
| `main/CMakeLists.txt` | board → `BOARD_DIR` + fonts; `shuttle` added to main's requirements |
| `main/application.cc` | 1) forward unknown JSON type `"shuttle"` to `ShuttleHandleIncomingJson()`; 2) always use WebSocket protocol; 3) optional skip of the OTA check (`CONFIG_SHUTTLE_SKIP_OTA_CHECK`) — all under `#if CONFIG_SHUTTLE_ENABLE`/`..._SKIP_OTA_CHECK` |

## Updating to a newer upstream

```bash
tools/update_xiaozhi.sh <commit-or-tag>      # from the repo root
```

The script re-vendors upstream into `firmware/`, keeps Shuttle's new files, re-applies
`firmware/upstream.patch` with `git apply --reject` (conflicts end up in `*.rej`). Afterwards:

1. Fix conflicts (usually `main/CMakeLists.txt` / `main/application.cc`), keep `SHUTTLE:` blocks.
2. Diff the upstream waveshare board (`main/boards/waveshare/esp32-s3-epaper-1.54/`) against
   the previous commit; port changes in `waveshare-s3-epaper-1.54.cc` to `shuttle_board.cc`
   (the driver files are included directly and need no porting).
3. `tools/make_upstream_patch.sh` to refresh `upstream.patch`, update the commit above.
4. Build (`python build_shuttle.py`) and run upstream's `python -m unittest discover -s scripts/tests`.
