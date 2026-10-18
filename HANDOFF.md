# HANDOFF — 搞快 Shuttle

## 0. State on 2026-10-18 (first day on real hardware)

Bring-up done on the dev Mac (M5 Max, macOS 26) with the board on USB and a Redmi K30 Pro
(Android 12, MIUI 14) on USB. **End-to-end voice control works**: hold the light-bulb key,
"打开设置" → Qwen3-ASR → vision LLM agent → adb → phone opens Settings → spoken reply.

| Part | Status |
|---|---|
| Hardware | Waveshare **ESP32-S3-Touch-ePaper-1.54 V2** (N8R8, touch FT6336 @0x38, INT21/RST7). Factory image backed up to `~/esp/backup/factory_full_8MB.bin` (restore: `esptool write-flash 0 …`). |
| Firmware | Runs. Wi-Fi (2.4 GHz "515-2"), OTA/config check, ctrl channel over LAN (`ws://192.168.1.7:8780`), PTT audio, TTS playback, **octopus mascot UI** (branch `claude/tender-fermi-wsyn89` merged and reworked: no state words, mood = state, speech bubble), touch (tap = cancel / details, long press = full refresh), PWR×2 = Wi-Fi portal. From the branch, **not yet exercised on hardware**: phone scanner / multi-phone switching / router mode (home network has `PHONE_HOST=none`, the phone is on the Mac's USB). |
| Server | LaunchAgent `com.shuttle.server` (+ watchdog), log `~/Library/Logs/shuttle/shuttle.log`. ASR = mlx-audio Qwen3-ASR (:8765, ~0.15 s), agent = `LlmAgent` on Splash Qwen3.8-27B vision (:8000, ~2.8 s per step incl. one 720×1600 screenshot), TTS = `say` Tingting. Branch features merged: phone registry, voice commands 换手机 etc. 53 pytest tests pass. Shuttle does **not** use the user's `pi` CLI or its config (`~/.pi`). |
| Phone | USB, `adb.fallback_serial: eceb1e32`. Input injection, `screencap`, phone helper (JPEG screenshot + clipboard Chinese input) all work on MIUI 14 (Telegram message sent by voice). Lock screen has a PIN → `stay_on_while_plugged_in=7` for testing. |

Fixed on day 1: TTS lost its beginning (xiaozhi drops audio until its switch to "speaking" is
handled, which waited for an e-paper refresh) → the server starts audio 0.8 s after "tts start"
and the display never blocks on SPEAKING; the full refresh ("flash") no longer runs right after
a task. Agent tool calls use `point_2d: [x, y]` (Qwen3.8 put pairs into `x` otherwise).

Microphone audio (ASR misheard short commands, first words swallowed; "电池状态" → "翅状"), fixed
the same evening — measured with the debug console (`ptt` while a full refresh runs, channel
closed): first packet 1.77 s after "listen start" and half the audio lost → **0.12 s, 3.24 s of
audio in a 3.33 s press**:
1. The Waveshare driver's BUSY wait (`vTaskDelay(pdMS_TO_TICKS(5))` = `vTaskDelay(0)` at 100 Hz)
   spun a CPU core for every e-paper refresh; the Opus encoder dropped frames (40 packets for a
   6 s press with the animated listening mascot). One-line `SHUTTLE:` patch in the driver.
2. Logs were mirrored to UART0 at 115200 baud (a screen dump = 0.6 s busy) → console USB-only.
3. The main loop waited for the LVGL lock (up to 2 s) before starting the mic and when the
   channel opened (phone icons) → those UI updates never wait now.
4. ES8311 power-up click (~50 ms full scale, ASR heard "嗯") muted by `ShuttleAudioCodec`.
5. Listening/speaking moods are static (no refresh while the mic or speaker is in use).

Same evening, the ESP32 as the bridge (the target architecture: phone ⇄ Wi-Fi ⇄ ESP32 ⇄ Mac, no
cables) works at home:
* Cloudflare Tunnel `mac-local` (remotely managed, LaunchAgent `com.cloudflare.cloudflared`,
  hostname in `server.public_url`), WAF path allow-list in the dashboard; server hardened
  (`/status`, `/api/*` local-only, bad-token lockout, minimal `/health` outside).
* From mainland China the board's direct route to Cloudflare is bad in the evening (300 ms,
  10 % loss) → per-network server URL: on "515-2" the board talks to `ws://192.168.1.7:8780`.
* Bridge throughput was ~2 KB/s with 10-30 s stalls → upstream Wi-Fi/lwIP buffers (3 RX buffers)
  raised in the board `config.json`, Wi-Fi power save off while ADB bytes flow, periodic phone
  scan only every 5 min while a phone is in use: now ~1 MB/s (2 MB in 2 s), screenshots 0.5-2 s.
* Agent: `phone_info` tool (battery/network/storage/apps without Settings), paging cap, notes;
  spoken replies ≤ 60 chars; child-like voice (`tts.pitch_semitones: 4`).
* The user unlocks the phone before giving commands (no PIN entry by the agent, by choice); the
  phone's screen times out after 10 min (MIUI maximum).

Measured: first audio 0.1–0.3 s after "listen start" (channel open: +0.1 s on the LAN); ASR 0.13–0.22 s;
"打开设置" via the LLM ≈ 12 s, via the fast path (bare "打开<应用>") ≈ 3 s; Mac-side screenshot
0.25–1 s over USB, phone-side helper 1.8 s incl. first push.

### Next steps (in order)

1. Remote use (the actual pocket scenario): the tunnel is up and the bridge works on the LAN.
   Left: Wi-Fi #2 = the phone's hotspot (2.4 GHz, SSID/password in `shuttle.local.conf`), then a
   real test outside - through Cloudflare from mobile data in mainland China and from Taiwan
   (expected fast: Taipei PoP). If the mainland route is too slow: a relay in China/HK.
   `adb tcpip 5555` is lost on every phone reboot (needs USB once).
2. Front of the utterance: still 0.1–0.3 s after the press before audio flows (codec power-up,
   upstream's 120 ms warm-up, the 60 ms click mute). Options: keep the codec input on while the
   ctrl channel is open (also allows pre-roll), or pre-open the ctrl channel after boot (needs a
   tiny `SHUTTLE:` hook in `application.cc`; matters most over TLS/Cloudflare, connect ~1 s).
   Confirm with the user's own voice that "电池状态" etc. are recognised now.
3. Agent quality: multi-step tasks (search, messaging) still to be exercised; Chinese text input
   path = helper clipboard (works) → ADBKeyboard fallback (IME switch implemented, untested).
4. Other phone brands (user wants to try): check input injection / clipboard per ROM.

## 1. Original cloud-side notes: unverified assumptions & TODOs (highest risk first)

Items 2, 3, 5, 6 (partly), 9, 10 (foreground `say` works), 11 were verified or replaced on
2026-10-18 (see §0); kept for reference.

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
4. **Phone discovery + several phones** — `phone_scanner.cc`, `adb_bridge.cc`, server
   `adb/phones.py`, `pipeline/phone_commands.py`. No phone IP is configured: the board scans its
   /24 for :5555 (8 parallel non-blocking connects, 700 ms, priority hosts first) and verifies
   adbd with a CNXN probe (expects AUTH/CNXN/STLS; must **not** show a dialog on the phone — verify
   once); hello → server picks by serial (`phones.json`) → `connect`. Untested on hardware: how
   long a full scan really takes, whether phones in Wi-Fi power save answer within 700 ms
   (`CONFIG_SHUTTLE_PHONE_SCAN_TIMEOUT_MS`), lwIP socket/ARP limits during a scan
   (`CONFIG_LWIP_MAX_SOCKETS=24`, batch 8 < ARP table 10), the close-code-4005 path in
   esp_websocket_client (a `{"type":"switch"}` text frame is sent too), the first "Allow USB
   debugging?" prompt after switching to a new phone. Simulated end to end in
   `server/tests/test_phones.py` (two fake adbds, `sim_device.py` speaks the same protocol).
5. **Wi-Fi priority** — `shuttle_wifi.cc`, `shuttle_config.cc`.
   Each Kconfig network carries `PHONE_HOST`: empty = find phones automatically (default),
   IP = only that phone, `none` = no ADB. esp-wifi-connect 3.3.1 connects to
   the strongest known AP; we emulate priority by temporarily restricting SsidManager's list (NVS
   rewrite, only while the station is stopped) and restarting the station: boot tries #1 for 20 s;
   while idle on a lower network it retries higher ones after 300 s, doubling to 1 h while absent
   (≈20 s offline per check; skipped while ADB traffic flowed in the last 60 s); **BOOT double-click**
   checks immediately. Untested on hardware: real switching times, whether the 20 s offline
   window disturbs anything, phones that cannot be Wi-Fi client while their hotspot is on.
   Portal-added SSIDs are kept in NVS `shuttle/extra_ssids` (phones found automatically). The upstream
   "no Wi-Fi for 60 s → config portal" timeout is **not** used.
   * PTT starts on press (a 250 ms hold delay for BOOT gestures cut off the first words);
     key gestures (best Wi-Fi, portal) are on PWR.
6. **Hardware facts taken from upstream, not measured** — `waveshare/esp32-s3-epaper-1.54/config.h`,
   cross-checked with waveshareteam/ESP32-S3-ePaper-1.54 V1/V2 examples (V1 and V2 differ only in
   flash/PSRAM; pins identical): ES8311 @ I2C SDA47/SCL48, I2S MCLK14 BCLK15 WS38 DIN16 DOUT45,
   PA 46, EPD SPI3 DC10 CS11 SCK12 MOSI13 RST9 BUSY8, power EPD 6 / audio 42 / VBAT hold 17,
   BOOT 0, PWR 18, battery ADC1_CH3 (GPIO4, ×2 divider), LED 3. docs.waveshare.com itself was not
   reachable from the cloud environment.
   * Battery %: upstream quadratic formula; **charging state is not detectable** (always "discharging").
   * Audio 24 kHz in/out at the codec (upstream); xiaozhi resamples mic to 16 kHz.
   * SHTC3, RTC (PCF85063), TF card and the touch variant are unused.
7. **xiaozhi channel keepalive** — server sends `{"type":"shuttle","event":"ping"}` every 30 s
   (resets xiaozhi's 120 s receive timeout) and WS pings (`server.ctrl_heartbeat_s`; xiaozhi's
   `web_socket.cc` answers pings — read in source, not observed). Idle channel closed after 600 s.
8. **OTA check at boot** — server answers `/xiaozhi/ota/` with time + same version +
   `websocket: {}`. If the server is unreachable at boot, xiaozhi retries up to 10× with growing
   delays and shows an error; pressing BOOT skips it (state → idle; WebSocket is forced by the
   `SHUTTLE:` patch). Alternative: `CONFIG_SHUTTLE_SKIP_OTA_CHECK=y`.
9. **Phone helper** (`server/phone_helper`): raw `screencap` header (12 vs 16 bytes),
   `Bitmap` under `app_process`, `IClipboard.setPrimaryClip` signatures (scrcpy technique),
   ROM restrictions (MIUI/HyperOS "USB debugging (Security settings)"). Fallbacks exist
   (Mac compression; ADBKeyboard; `input text` for ASCII).
10. **Qwen ASR API shape** — assumed OpenAI-compatible (`/v1/audio/transcriptions` or
   `/v1/chat/completions` with `audio_url`) as served by vLLM; adapt `pipeline/asr.py` to the
   actual local deployment.
11. **macOS specifics** — `say -o … --data-format=LEI16@24000` from a **LaunchDaemon** (no GUI
    session) is untested; if it fails use `install.sh --agent` (+ auto-login) or another TTS.
    `pmset autorestart` applies to desktop Macs. Homebrew paths assumed `/opt/homebrew`.
12. **Agent** — `SimpleAgent` is a rule-based placeholder demonstrating the loop; integrate the
    pi agent via `agent.provider: python|command` (`docs/README.md` §2.1).
13. **PWR key** — unchanged from upstream (long press = power off). GPIO18 is both the PWR key
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
   `idf.py flash monitor`. Start **at home** (phone on home Wi-Fi with `adb tcpip 5555`; easiest
   to debug), then hotspot, then switching (leave/come home, PWR click), then a second phone
   (`adb tcpip 5555` on it → board switches within a minute; "换手机"; "现在连的是哪台手机").
   ✅ serial: `PhoneScanner: Scan of 192.168.1.0/24: … 1 phone(s) [192.168.1.50] in … ms`,
   `AdbBridge: Server chose phone 192.168.1.50`, `State … -> active`; server log `hello on '<ssid>' … (reason)`,
   `phone at … is <model>`; Mac: `adb devices` → `device` (accept RSA prompt first time); e-paper subtitle = phone name;
   `curl …/api/phone/screenshot` works through the ESP32. Measure screenshot latency.
5. **Voice up/down** — hold BOOT, say "打开设置", release. ✅ server log `listen start/stop`,
   `utterance N ms`, ASR text; device plays the reply; e-paper 录音中 → 思考中 → 执行中 → 完成.
6. **End-to-end** — multi-step commands, Chinese text input ("搜索 天气"), interrupting with a new
   press, phone reboot (→ "未发现手机" until `adb tcpip 5555`), Mac reboot/power cut (service
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
