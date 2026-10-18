# CLAUDE.md — 搞快 Shuttle

Guide for Claude Code working in this repo (usually on the remote Mac, with the ESP32 board and
the Android phone at hand). Read `HANDOFF.md` first for the open TODOs / bring-up order.

## What this is

A pocket ESP32-S3 e-paper device that voice-controls an Android phone. Hold BOOT (the
light-bulb key), speak, release → the Mac server runs ASR → agent → TTS; the agent (a local
vision LLM) sees and drives the phone through **native adb on the Mac** — either directly
(USB / LAN, `adb.fallback_serial`) or tunnelled byte-for-byte by the ESP32
(phone hotspot ⇄ ESP32 ⇄ Internet/Cloudflare ⇄ Mac). No app on the phone.

**Hardware (verified 2026-10-18):** Waveshare *ESP32-S3-Touch-ePaper-1.54* V2 — ESP32-S3-PICO-1,
8 MB flash + 8 MB octal PSRAM, 200×200 e-paper, ES8311 codec, **FT6336 touch** (I2C 0x38,
INT GPIO21, RST GPIO7), PCF85063 RTC (0x51), SHTC3 (0x70). Keys: light bulb = BOOT/GPIO0
(hold = push-to-talk, recording starts on press), power = GPIO18 (click = best Wi-Fi now,
×2 = Wi-Fi portal, long = off). Factory flash backup:
`~/esp/backup/factory_full_8MB.bin` on the dev Mac.

```
ESP32 (firmware/)                       Mac (server/)
 BOOT PTT, Opus 16k ──/ws/ctrl (xiaozhi protocol)──▶ CtrlSession → ASR → Agent → TTS (Opus 24k)
 TCP phone:5555 ◀═══/ws/adb (raw bytes)═════════▶ AdbTunnel ⇄ 127.0.0.1:15555 ⇄ adb ⇄ PhoneController
 (phones found by scanning the Wi-Fi; hello{candidates} → server picks by serial → connect{host})
 e-paper status ◀── {"type":"shuttle","event":"status"|"phone"|"ping"} ──
```

Protocol spec: `docs/protocol.md`. Setup: `docs/README.md`.

## Layout

| Path | What | Ownership |
|---|---|---|
| `firmware/` | ESP-IDF v6.1 project = vendored **xiaozhi-esp32** @ d7a1c05 + Shuttle additions | mostly upstream |
| `firmware/extra_components/shuttle/` | Shuttle component: `Kconfig`, `shuttle_config` (settings, NVS provisioning, last phone), `phone_scanner` (finds adbd on the /24, CNXN probe), `adb_bridge` (TCP⇄WS bridge, hello/connect/switch, PSRAM buffers, backoff), `debug_console` (USB command console for tests) | **ours** |
| `firmware/main/boards/shuttle/epaper-1.54-v2/` | board: `shuttle_board.cc` (PTT, battery, bridge, JSON hook, touch gestures, console commands, ES8311 click mute), `shuttle_ui.*` (octopus mascot UI, pure LVGL), `shuttle_display.*` (xiaozhi Display → UI mapping, e-paper refresh policy), `mascot_frames.h` (generated), `shuttle_touch.*` (FT6336), `shuttle_wifi.*` (priority Wi-Fi), `waveshare_bsp.cc` (includes upstream waveshare drivers) | **ours** |
| `tools/mascot/gen_mascot.py`, `tools/ui_sim/` | mascot pixel art → `mascot_frames.h`; desktop LVGL renderer of the real UI → `docs/ui/` (see `docs/ui/README.md`) | ours |
| `firmware/build_shuttle.py`, `shuttle.local.conf(.example)`, `UPSTREAM.md`, `upstream.patch` | build wrapper, private settings, upstream sync notes | **ours** |
| `firmware/main/**` (everything else), `scripts/`, `partitions/` … | xiaozhi upstream | **do not edit** (see below) |
| `server/shuttle_server/` | Python 3.11+ aiohttp server | **ours** |
| ├ `app.py` | routes `/ws/ctrl` `/ws/adb` `/xiaozhi/ota/` `/health` `/status` `/api/phone/*`, wiring | |
| ├ `config.py` | dataclass config (`config.yaml`), env overrides, validation | |
| ├ `adb/adb_cli.py` | async `adb` subprocess wrapper with timeouts | |
| ├ `adb/tunnel.py` | `AdbTunnel`/`TunnelManager`: local port ⇄ device WS, single-use sessions, `adb connect` supervisor, hello → connect, switch (4005), phone identification | |
| ├ `adb/phones.py` | `PhoneRegistry` (serial → name/last IP/last used, `phones.state_file`), `choose_host` policy | |
| ├ `adb/phone.py` | `PhoneController`: screenshot (≤200 KB JPEG, short side 720), tap/swipe/text/back/home/dump_ui, coordinate scaling | |
| ├ `adb/phone_info.py` | battery / network / storage / installed apps straight from dumpsys, getprop, pm (agent tool `phone_info`) | |
| ├ `auth.py` | token check, `is_local` (no `CF-Ray`, loopback), `AuthGuard` bad-token lockout | |
| ├ `pipeline/session.py` | `CtrlSession`: xiaozhi protocol, utterance → ASR → (phone commands) → agent → TTS, status pushes | |
| ├ `pipeline/phone_commands.py` | "换手机" / "切换到X" / "哪台手机" + `/api/phones` actions | |
| ├ `pipeline/{asr,tts,agent}.py` | pluggable interfaces + Qwen/mock ASR, say/openai/command/mock TTS, simple/echo/command/python agents | |
| ├ `pipeline/llm_agent.py` | `LlmAgent` (default): vision LLM + tool calls (tap/swipe/type/launch/back/home/wait/finish), 0..1000 coords, fast path for "打开<应用>" | |
| ├ `pipeline/apps.py` | spoken app name → installed package (`pm list packages`), brand-specific system apps | |
| └ `audio/` | ctypes libopus binding, PCM/WAV helpers | |
| `server/phone_helper/` | optional on-phone helper (Java → dex, run via `app_process`): JPEG screenshots, clipboard for Chinese input | ours |
| `server/tests/` | pytest (fake adb, tunnel, phone, e2e) | ours |
| `tools/sim_device.py` | simulates the ESP32 (ctrl + ADB bridge with phone scan/hello, several `--phone`, mic/WAV/`say`) | ours |
| `tools/esp_console.py`, `tools/flash_shuttle.sh` | serial console / flashing that work with the macOS USB-JTAG quirk (see Gotchas) | ours |
| `tools/screen_dump.py` | decode e-paper frames from the serial log (console `dump`, or `CONFIG_SHUTTLE_DEBUG_SCREEN_DUMP=y`) into PNG | ours |
| `tools/fake_adb.py` | fake `adb` binary simulating a phone | ours |
| `tools/update_xiaozhi.sh`, `tools/make_upstream_patch.sh` | upstream sync | ours |
| `deploy/` | `install.sh`, launchd plists (server + watchdog), cloudflared example, `macos-unattended.sh`, `healthcheck.sh` | ours |
| `.github/workflows/` | `firmware.yml` (esp-idf-ci-action v6.1 → merged-binary.bin), `server.yml` (pytest, phone-helper jar) | ours |

## Code conventions

* **xiaozhi code is upstream.** Only 5 upstream files are patched, each block marked `SHUTTLE:`
  (`firmware/UPSTREAM.md`, exact diff in `firmware/upstream.patch`). Put new behaviour in the
  Shuttle component or board directory. If you must touch upstream, keep it tiny, mark it
  `SHUTTLE:`, then run `tools/make_upstream_patch.sh`.
* Upstream rules from `firmware/AGENTS.md` apply: don't block the main loop or audio tasks, change
  device state only via `Application::SetDeviceState`, schedule UI work with `Application::Schedule`,
  keep the board menu in `main/Kconfig.projbuild` sorted (upstream unit test checks it).
* An e-paper refresh holds the LVGL lock 0.3 s (partial) / 2 s (full). UI calls from the main
  loop (it starts the mic, sends the audio, handles the next key press) or the esp_timer task (it
  polls the keys) must not wait for it: `ShuttleDisplay` uses `TryLock()` + deferral to its
  200 ms refresh timer (`ApplyStatus`, `RunOrDefer`, atomics). Keep new calls on those paths so.
* All configuration is central: firmware → `extra_components/shuttle/Kconfig` (read only through
  `shuttle/shuttle_config.h`); server → `config.py` / `config.yaml`. No hard-coded URLs/tokens.
* Firmware C++: match upstream style (`.clang-format`, 4 spaces, `ESP_LOGx` with a `TAG`).
  Log every key event (Wi-Fi, bridge state changes, PTT, task status) — serial log is the main
  debugging tool.
* Server: asyncio only, every external command has a timeout, never let one device/agent error
  kill the process (log + status `error`). Coordinates exposed to agents are screenshot pixels.
* Secrets (`shuttle.local.conf`, `server/config.yaml`) are git-ignored. Never log tokens
  (`config.mask()`, `MaskSecret`).

## Commands

```bash
# --- firmware (ESP-IDF v6.1; on the dev Mac: PATH=~/esp/pybin:$PATH; . ~/esp/esp-idf/export.sh) ---
cd firmware
python build_shuttle.py                         # build (uses shuttle.local.conf), zh-CN, wake word off
../tools/flash_shuttle.sh                       # flash + boot (NOT idf.py flash: see Gotchas)
python ../tools/esp_console.py                  # serial log without resetting the board (--reboot to restart)
python ../tools/esp_console.py --log ~/esp/serial.log -q --fifo ~/esp/serial.in &   # background logger
echo "ptt 3000" > ~/esp/serial.in               # debug console (CONFIG_SHUTTLE_DEBUG_CONSOLE=y): ptt <ms>,
                                                #   tap, hold, pwr, dump, stats <ms>, reboot
python ../tools/screen_dump.py ~/esp/serial.log -o screen.png    # what the e-paper shows (after console "dump")
idf.py menuconfig                               # Component config → Shuttle (搞快)
python -m unittest discover -s scripts/tests    # upstream build-tooling tests
esptool.py --chip esp32s3 erase_flash           # wipe NVS (Wi-Fi, provisioned URL/token)

# --- server ---
deploy/install.sh                               # first install / update (idempotent)
sudo launchctl kickstart -k system/com.shuttle.server        # restart service (dev Mac: --agent install,
launchctl kickstart -k gui/$(id -u)/com.shuttle.server       #   i.e. this one)
sudo launchctl bootout system/com.shuttle.server             # stop (bootstrap to start again)
tail -f ~/Library/Logs/shuttle/shuttle.log
cd server && .venv/bin/python -m shuttle_server -c config.yaml     # foreground (service stopped)
# Cloudflare Tunnel (remotely managed, LaunchAgent com.cloudflare.cloudflared): log
#   ~/Library/Logs/com.cloudflare.cloudflared.err.log ("Registered tunnel connection"); docs/README.md §3
# local AI services on the dev Mac (LaunchAgents com.local.*): Splash :8000 Qwen3.8-27B **vision**
# (agent), Splash :8001 Qwen3.8-27B heretic **text-only** (not for the agent), mlx-audio :8765
# Qwen3-ASR-1.7B-8bit (ASR). Shuttle itself listens on :8780.
cd server && .venv/bin/python -m pytest -q      # tests (need libopus: brew install opus)
curl -s localhost:8780/health; curl -s -H "Authorization: Bearer $TOKEN" localhost:8780/status | jq

# --- simulator (no ESP32 needed) ---
python tools/sim_device.py --url ws://127.0.0.1:8780 --token $TOKEN --say "打开设置" --play
python tools/sim_device.py --token $TOKEN --phone <phone-ip>:5555 --bridge-only   # be the ADB bridge
python tools/sim_device.py --token $TOKEN --phone <ip-a> --phone <ip-b> --bridge-only   # two phones
python tools/sim_device.py --token $TOKEN --mic --play                            # PTT with mic (pip install sounddevice)

# --- phone/adb ---
adb devices; adb connect 127.0.0.1:15555; adb -s 127.0.0.1:15555 shell input tap 500 500
curl -H "Authorization: Bearer $TOKEN" localhost:8780/api/phone/screenshot -o s.jpg
curl -H "Authorization: Bearer $TOKEN" localhost:8780/api/phones | jq          # phones found / current
curl -XPOST -H "Authorization: Bearer $TOKEN" localhost:8780/api/phones/next  # switch phone
server/phone_helper/build.sh                    # optional helper jar (needs Android SDK)
```

## Debugging by layer

1. **Wi-Fi** (serial `ShuttleWifi`, `WifiStation`, `ShuttleBoard`): "Network #n" list at boot,
   "Trying higher-priority…", "Wi-Fi '<ssid>' ip=… gw=…, phone: auto(scan)|fixed <ip>|none".
   2.4 GHz only (home "515" is 5 GHz only → firmware uses "515-2"). Portal: double-click PWR
   (AP `Shuttle-XXXX`, http://192.168.4.1). PWR click = switch to the best network now (BOOT
   has no gestures: PTT starts on press, a hold delay cut off the first words).
1b. **Phone discovery / choice** (serial `PhoneScanner` "Scan of …/24: N open, M phone(s) [...]",
   `AdbBridge` "Server chose phone …"; server `hello on '<ssid>' … -> host (reason)`, `phone at … is
   <name>`; `GET /api/phones`). No IPs are configured (`PHONE_HOST` empty = scan); the server picks
   by serial (`phones.json`): requested > new phone > pinned > last used > gateway. 🏠 icon = phone
   in use is not the hotspot. A phone on USB/LAN of the Mac (`adb.fallback_serial`) is reported
   to the board as link `mac`.
2. **ADB bridge** (serial `AdbBridge`; server `shuttle.adb.tunnel`; `/status` → `tunnels`):
   device states `connecting_server → connecting_phone → active`. HTTP 401 = token mismatch;
   `phone_unreachable` = no `adb tcpip 5555` / client isolation. On the Mac:
   `adb devices` should list `127.0.0.1:15555 device`; `unauthorized` → accept on phone.
   Throughput check: `adb -s 127.0.0.1:15555 exec-out "head -c 2000000 /dev/zero" | wc -c`
   (~2 s on the LAN). Serial "ADB traffic flowing/idle: Wi-Fi power save off/back to normal"
   shows the board's power-save switching; "WebSocket send failed (0/N)" = the board's upload
   stalled (buffers, power save, a lossy route). A dozing phone may miss a scan (next one finds it).
   Test the Mac half alone with `sim_device.py --phone <ip>:5555 --bridge-only` from a laptop on
   the phone's hotspot, or the phone half with `adb connect <phone-ip>:5555` directly.
3. **Audio** (serial `Application`, `AudioService` — "Encode queue is full" = Opus encoder
   starved, frames lost; server `shuttle.pipeline.session`): "utterance N ms: P packets in T s
   (first after F s)" — healthy is N ≈ T − F with F ≈ 0.1–0.3 s. `min_utterance_ms` drops taps,
   filler-only ASR ("嗯。") is answered with 没听清. `CONFIG_SHUTTLE_PTT_TAIL_MS` keeps recording
   after release. Test without touching the board: debug console `ptt 3000` (+ `say …` near it,
   `stats 1500` for per-task CPU), `voice.save_utterances_dir` keeps the WAVs. Reproduce
   server-side with `sim_device.py --wav`. Reply audio: `--save-tts out.wav`.
4. **ASR**: log `ASR (qwen/...) N ms: '…'`; swap in `asr.provider: mock` to isolate.
5. **Agent / phone**: log `agent step …`, `tap (x,y) -> device (X,Y)`, `screenshot WxH … via mac|phone`.
   Call the phone API by hand (curl above). `agent.provider: echo` isolates voice from the phone.
6. **TTS**: `TTS failed:` in the log; `tts.provider: mock` produces beeps.
7. **E-paper** (serial `ShuttleDisplay`: "Mood: …", "Status: …", "Full refresh …"): every LVGL
   flush = one partial refresh. The octopus mood *is* the state (no state words). Full refresh
   when due (150 partials / 30 min) but only after 30 s of quiet idle, never during an animation or
   right before TTS; long press on the screen forces one. Connecting/listening/speaking never wait
   for the LVGL lock (see Code conventions): a wait there cut off the first words / the reply.
   Iterate on layout/art without hardware: `tools/ui_sim/run.sh` renders the real `shuttle_ui.cc`
   to `docs/ui/screens.png`; on the device the console `dump` (or
   `CONFIG_SHUTTLE_DEBUG_SCREEN_DUMP=y`, every frame) + `tools/screen_dump.py` show what the
   panel shows — use these instead of asking for photos.
8. **Touch** (serial `ShuttleTouch`: "Tap at (x,y)"): tap while busy = cancel (sends xiaozhi
   `abort`, server cancels the agent), tap while idle = details page, long press = full refresh.

## Gotchas

* `build_shuttle.py` regenerates `sdkconfig` from defaults each run — persist settings in
  `shuttle.local.conf`, not via a one-off menuconfig.
* Flash the **merged** image (or `idf.py flash`): the assets partition carries the CJK
  ("common") font; without it most Chinese UI strings render as blanks.
* xiaozhi stores the WebSocket URL/token in NVS; Shuttle rewrites them from Kconfig at every
  boot (`ProvisionXiaozhiSettings`). The OTA reply must keep `"websocket": {}`.
* The ctrl channel is opened on the first BOOT press (TLS takes ~1 s); afterwards the server's
  keepalive keeps it open for `voice.idle_close_s`.
* `adb tcpip 5555` is lost on every phone reboot. `adb -s <ip>:5555 usb` turns it off without a reboot
  (deliberately not a voice command: it can only be turned back on with a USB cable).
* Switching phones keeps the adb serial `127.0.0.1:15555`; per-phone caches in `PhoneController`
  are dropped via `AdbTunnel.phone_generation`. A new phone shows "Allow USB debugging?" once.
* **macOS + USB-Serial/JTAG:** opening the port raises DTR+RTS together, which the S3 treats as
  "reset into download mode" → `idf.py monitor`/plain pyserial leave the board at "waiting for
  download", and an RTS hard reset after flashing has the same effect once the port is re-opened.
  Use `tools/esp_console.py` (opens with DTR/RTS already asserted, reboots via RTC watchdog) and
  `tools/flash_shuttle.sh` (`--after watchdog-reset`). A chip reset re-enumerates USB: the first
  ~4 s of boot log are usually missed (the board re-logs the I2C scan once Wi-Fi is up).
* ESP-IDF v6.1 refuses Python 3.15 (Homebrew default on the dev Mac): `~/esp/pybin/python3` →
  Python 3.12 must come first in PATH before sourcing `export.sh`.
* Port 8765 is taken by the mlx-audio ASR server on the dev Mac; Shuttle uses 8780.
* The 20 px CJK font has a ~31 px line height: the message area uses negative line spacing.
* Upstream `build.py` enables a wake word by default; `build_shuttle.py` passes `--wake-word disabled`.
* Upstream's Wi-Fi/lwIP buffers are sized for voice (3 static RX buffers, BA window 3, TCP
  mailbox 6): the ADB bridge dropped packets, stalled 10-30 s and timed out on every screenshot.
  The board `config.json` raises them (8/32 RX buffers, BA 16, mailboxes 32, TCP window 32 KB,
  Wi-Fi/lwIP memory in PSRAM): ~1 MB/s through the board. Keep them if you touch `config.json`.
* xiaozhi switches Wi-Fi power save off only while the voice channel is open; the board also
  switches it off while ADB bytes flow (`ShuttleBoard::OnPowerSaveTimer`), else ~2 KB/s.
* Per-network server: `CONFIG_SHUTTLE_WIFI<n>_SERVER_URL` (e.g. the Mac's LAN address at home)
  is applied on every Wi-Fi connect (`SetCurrentNetwork` + `ProvisionXiaozhiSettings` + bridge
  `SetServerUrl`). The server must then listen on the LAN (`server.host: 0.0.0.0`).
* The public hostname is guarded twice: Cloudflare WAF (only `/ws/ctrl`, `/ws/adb`,
  `/xiaozhi/ota`, `/health`) and the server (`/status`, `/api/*` only for local programs
  unless `server.remote_api`; bad-token lockout `AuthGuard`). Tests: `tests/test_security.py`.
* FreeRTOS tick is 100 Hz: `pdMS_TO_TICKS(x)` is 0 for x < 10, and `vTaskDelay(0)` is a busy loop
  (the Waveshare driver's BUSY wait spun a core for every refresh until patched; audio dropped).
* The console is USB-Serial/JTAG only (board `config.json`). Upstream's UART0 mirror at 115200
  throttled every log line (a 7 KB screen dump = 0.6 s of a busy core).
* ES8311 power-up click: the ADC is powered down after 15 s without recording; `ShuttleAudioCodec`
  (board) mutes the first 60 ms after it powers up (upstream's warm-up runs before that).
* Agent answers about the phone itself (battery, Wi-Fi, storage, installed apps) come from the
  `phone_info` tool (`adb/phone_info.py`, dumpsys/getprop/pm) - no Settings navigation. Paging is
  capped (`MAX_SCROLLS_IN_A_ROW`/`MAX_SCROLLS`): the model kept scrolling past the wanted item.
* Spoken replies are capped at `voice.max_spoken_chars` (cut at a sentence end, screen shows all);
  `tts.pitch_semitones: 4` = the child-like voice (ffmpeg asetrate + atempo on any TTS provider).
* MIUI: input injection needs *USB debugging (Security settings)*; the phone must be unlocked
  (secure lock → the agent answers 手机锁着). Dev phone: `stay_on_while_plugged_in=7`.
