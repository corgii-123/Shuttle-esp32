# HANDOFF — 搞快 Shuttle

State at hand-off (written in a cloud environment **without** the board, the phone or the Mac):

| Part | Status |
|---|---|
| Firmware | Complete. **Compiles in CI** (GitHub Actions *Firmware*, ESP-IDF v6.1, esp-idf-ci-action) → `merged-binary.bin` artifact (app 2.7 MB, 10 % partition headroom). The `shuttle` component also compiles locally with `-Werror`. **Never run on hardware.** |
| Server | Complete. 35 pytest tests pass (Python 3.11 + 3.13 in CI): fake-adb phone control, byte-exact ADB tunnel with a simulated bridge, full voice → ASR(mock) → agent → adb → TTS(mock) chain. **Never run against a real phone, Qwen ASR or macOS `say`.** |
| Phone helper | Java compiles; dex jar built in CI (*Server* → artifact `shuttle-helper-jar`). **Never run on a phone.** |
| Deploy | `install.sh`, launchd plists, watchdog, cloudflared config written; **not executed on macOS** (bash syntax-checked only). |

## 1. Unverified assumptions & TODOs (highest risk first)

1. **ADB pass-through on real networks** — `extra_components/shuttle/adb_bridge.cc`.
   * Throughput/latency hotspot → Internet → Cloudflare → Mac. A `screencap -p` PNG (1–3 MB)
     may take seconds; enable `phone.device_side_screenshot` (helper) if slow. Tunables:
     `CONFIG_SHUTTLE_ADB_WS_BUFFER_SIZE` (8 KB), `..._TX_CHUNK_KB` (16), `..._RX_BUFFER_KB` (256),
     `CONFIG_LWIP_TCP_SND_BUF_DEFAULT/WND_DEFAULT` = 16384 (set in board `config.json`; the CI step
     "Show resolved Shuttle config" prints whether Kconfig accepted them). Wi-Fi RX buffers are
     upstream's small values (`sdkconfig.defaults.esp32s3`) — raising them may help.
   * One task `recv()`s and another `send()`s on the same lwIP socket (needs lwIP full-duplex,
     which ESP-IDF enables — verify no `EINPROGRESS`/assert under load).
   * The ws event handler blocks (backpressure) while the PSRAM stream buffer is full; check that
     pings still get answered under a long `adb pull`.
   * Gateway == phone on the hotspot: true for stock Android tethering; check for client isolation.
2. **Push-to-talk flow in xiaozhi manual mode** — `shuttle_board.cc` `InitializeButtons()`.
   First press opens TLS (~1 s, state 连接中); speech during that second is lost. Release while
   connecting is handled by `ptt_release_timer_`. Check short taps, press during TTS (abort +
   new utterance), press during an agent task (server cancels it).
3. **E-paper UI & refresh** — `shuttle_display.cc`.
   * Our layout on top of the upstream Waveshare driver (`CustomLcdDisplay`, every LVGL flush =
     full-frame *partial-waveform* refresh). Ghost-clearing full refresh = `EPD_Init();
     EPD_DisplayPartBaseImage(); EPD_Init_Partial();` from an LVGL timer — sequence derived from
     the driver's constructor, not tested. Policy: every 40 partials, after a task if ≥12, idle ≥30 min.
   * Counting uses `LV_EVENT_FLUSH_START` (LVGL 9.5).
   * Fonts: 4-bpp anti-aliased glyphs are thresholded to black/white by the driver (`< 0x7fff`).
     Waveshare ships patched `78__xiaozhi-fonts` for an *older* xiaozhi; upstream now supports the
     board with fonts 2.0, so we use upstream fonts — check legibility.
   * Chinese strings (录音中/思考中/执行中/完成/手机已连接…) exist only in the **assets** "common"
     font (verified in xiaozhi-fonts `charsets/common.json`); flash the merged image. Server-side
     `glyph_push` (xiaozhi feature) is not implemented — rare characters in replies may be blank.
   * UI = pixel-art mascot + state pill (`shuttle_ui.cc`, previews in `docs/ui/`, rendered by
     `tools/ui_sim` with LVGL 9.5 + the real fonts + the driver's black/white threshold). On the
     panel check: contrast of the 4-bpp CJK text after thresholding, ghosting of the inverted
     title pill, and whether mascot animations (0.6–1 s/frame partial refreshes) look smooth
     enough or should be slowed/disabled (`ShuttleUi::Timing`, `kAnims` intervals).
4. **Wi-Fi priority / router + hotspot modes** — `shuttle_wifi.cc`, `shuttle_config.cc`.
   Each Kconfig network carries `PHONE_HOST`: fixed IP = router mode (home Wi-Fi, phone at a
   reserved IP), empty = hotspot mode (phone = DHCP gateway). esp-wifi-connect 3.3.1 connects to
   the strongest known AP; we emulate priority by temporarily restricting SsidManager's list (NVS
   rewrite, only while the station is stopped) and restarting the station: boot tries #1 for 20 s;
   while idle on a lower network it retries higher ones after 300 s, doubling to 1 h while absent
   (≈20 s offline per check; skipped while ADB traffic flowed in the last 60 s); **BOOT double-click**
   checks immediately. Untested on hardware: real switching times, whether the 20 s offline
   window disturbs anything, phones that cannot be Wi-Fi client while their hotspot is on.
   Portal-added SSIDs are kept in NVS `shuttle/extra_ssids` (treated as hotspot mode). The upstream
   "no Wi-Fi for 60 s → config portal" timeout is **not** used.
   * BOOT gestures need PTT to start only after a 250 ms hold (`kPttHoldMs`): the first ~0.25 s
     of speech can be cut if the user talks instantly — tune if needed.
5. **Hardware facts taken from upstream, not measured** — `waveshare/esp32-s3-epaper-1.54/config.h`,
   cross-checked with waveshareteam/ESP32-S3-ePaper-1.54 V1/V2 examples (V1 and V2 differ only in
   flash/PSRAM; pins identical): ES8311 @ I2C SDA47/SCL48, I2S MCLK14 BCLK15 WS38 DIN16 DOUT45,
   PA 46, EPD SPI3 DC10 CS11 SCK12 MOSI13 RST9 BUSY8, power EPD 6 / audio 42 / VBAT hold 17,
   BOOT 0, PWR 18, battery ADC1_CH3 (GPIO4, ×2 divider), LED 3. docs.waveshare.com itself was not
   reachable from the cloud environment.
   * Battery %: upstream quadratic formula; **charging state is not detectable** (always "discharging").
   * Audio 24 kHz in/out at the codec (upstream); xiaozhi resamples mic to 16 kHz.
   * SHTC3, RTC (PCF85063), TF card and the touch variant are unused.
6. **xiaozhi channel keepalive** — server sends `{"type":"shuttle","event":"ping"}` every 30 s
   (resets xiaozhi's 120 s receive timeout) and WS pings (`server.ctrl_heartbeat_s`; xiaozhi's
   `web_socket.cc` answers pings — read in source, not observed). Idle channel closed after 600 s.
7. **OTA check at boot** — server answers `/xiaozhi/ota/` with time + same version +
   `websocket: {}`. If the server is unreachable at boot, xiaozhi retries up to 10× with growing
   delays and shows an error; pressing BOOT skips it (state → idle; WebSocket is forced by the
   `SHUTTLE:` patch). Alternative: `CONFIG_SHUTTLE_SKIP_OTA_CHECK=y`.
8. **Phone helper** (`server/phone_helper`): raw `screencap` header (12 vs 16 bytes),
   `Bitmap` under `app_process`, `IClipboard.setPrimaryClip` signatures (scrcpy technique),
   ROM restrictions (MIUI/HyperOS "USB debugging (Security settings)"). Fallbacks exist
   (Mac compression; ADBKeyboard; `input text` for ASCII).
9. **Qwen ASR API shape** — assumed OpenAI-compatible (`/v1/audio/transcriptions` or
   `/v1/chat/completions` with `audio_url`) as served by vLLM; adapt `pipeline/asr.py` to the
   actual local deployment.
10. **macOS specifics** — `say -o … --data-format=LEI16@24000` from a **LaunchDaemon** (no GUI
    session) is untested; if it fails use `install.sh --agent` (+ auto-login) or another TTS.
    `pmset autorestart` applies to desktop Macs. Homebrew paths assumed `/opt/homebrew`.
11. **Agent** — `SimpleAgent` is a rule-based placeholder demonstrating the loop; integrate the
    pi agent via `agent.provider: python|command` (`docs/README.md` §2.1).
12. **PWR key** — unchanged from upstream (long press = power off). GPIO18 is both the PWR key
    input and VBAT sense (upstream design).

## 2. Bring-up order (each step has a pass criterion)

1. **Stock xiaozhi firmware** — flash Waveshare's `03_Firmware/V2-XiaoZhi.bin`
   (waveshareteam/ESP32-S3-ePaper-1.54) or upstream `scripts/build.py waveshare/esp32-s3-epaper-1.54 --name esp32-s3-epaper-1.54-v2`.
   ✅ screen shows UI, mic and speaker work (talk to the public xiaozhi server), battery icon plausible.
2. **adb over the hotspot from a computer** — laptop joins the phone hotspot; `ip route`/`netstat -rn`
   gateway = phone; `adb connect <gw>:5555`; `adb exec-out screencap -p > s.png`; `adb shell input tap 500 500`.
   ✅ screenshot opens, tap visible. Note screenshot time.
3. **Server with the simulator** — `deploy/install.sh`; `tools/sim_device.py --say 打开设置 --play`
   (start with `asr: mock`, `tts: say`); then real Qwen ASR; then from the laptop on the hotspot:
   `sim_device.py --url wss://<host> --phone <gw>:5555 --say "打开设置"`.
   ✅ `/status` shows tunnel `active`, `adb devices` shows `127.0.0.1:15555 device`, the phone opens Settings, reply audio plays.
4. **Shuttle firmware: Wi-Fi + ADB bridge** — fill `shuttle.local.conf`, `python build_shuttle.py`,
   `idf.py flash monitor`. Start with **router mode at home** (phone with a reserved IP on home
   Wi-Fi; easiest to debug), then hotspot mode, then switching (leave/come home, BOOT ×2).
   ✅ serial: `Wi-Fi '<ssid>' ip=… gw=…, phone via router(fixed ip) -> 192.168.1.50`, `AdbBridge: State … -> active`;
   Mac: `adb devices` → `device` (accept RSA prompt first time); e-paper bottom "手机已连接";
   `curl …/api/phone/screenshot` works through the ESP32. Measure screenshot latency.
5. **Voice up/down** — hold BOOT, say "打开设置", release. ✅ server log `listen start/stop`,
   `utterance N ms`, ASR text; device plays the reply; e-paper 录音中 → 思考中 → 执行中 → 完成.
6. **End-to-end** — multi-step commands, Chinese text input ("搜索 天气"), interrupting with a new
   press, phone reboot (→ "ADB未开启" until `adb tcpip 5555`), Mac reboot/power cut (service
   back, device reconnects with backoff), hotspot off/on, home Wi-Fi fallback, 8 h idle (battery,
   ghosting, reconnects).

## 3. ESP-IDF on macOS & flashing

```bash
brew install cmake ninja dfu-util ccache python@3.12
git clone -b v6.1 --recursive --shallow-submodules --depth 1 https://github.com/espressif/esp-idf.git ~/esp/esp-idf
~/esp/esp-idf/install.sh esp32s3
. ~/esp/esp-idf/export.sh
cd firmware && cp shuttle.local.conf.example shuttle.local.conf && $EDITOR shuttle.local.conf
python build_shuttle.py
idf.py -p /dev/cu.usbmodem* flash monitor       # or: esptool.py --chip esp32s3 write_flash 0x0 build/merged-binary.bin
```
The board enumerates as USB-Serial/JTAG (`/dev/cu.usbmodem*`). No port → hold BOOT, reset/PWR, release.
CI image: Actions → *Firmware* → artifact → `esptool.py --chip esp32s3 write_flash 0x0 merged-binary.bin`
(contains placeholder URL/token unless repository secrets are set).

## 4. Known limitations / next steps

* Latency budget: TLS connect on first press; ASR; one screenshot + UI dump per agent step over
  the tunnel. Ideas: keep `/ws/ctrl` open permanently (pre-connect after boot), stream audio to
  ASR while still recording, phone-side screenshot by default, cache `uiautomator` output, use
  `exec-out` streaming for screenshots.
* Server-side `glyph_push` so arbitrary Chinese replies render on the e-paper.
* Stock xiaozhi auto mode uses a naive energy VAD on the server.
* Multiple devices are supported in principle (one tunnel/port per Device-Id) but untested.
* No TLS between cloudflared and the server (localhost only). The token is a single shared secret;
  rotate it in both `config.yaml` and the firmware.
* OTA via `ota.firmware_dir` exists but is untested; USB flashing is the reference path.
* Wake word is disabled (PTT only); xiaozhi supports enabling one via `build.py --wake-word`.
