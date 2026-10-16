# CLAUDE.md — 搞快 Shuttle

Guide for Claude Code working in this repo (usually on the remote Mac, with the ESP32 board and
the Android phone at hand). Read `HANDOFF.md` first for the open TODOs / bring-up order.

## What this is

A pocket ESP32-S3 e-paper device that voice-controls an Android phone. Hold BOOT, speak,
release → the Mac server (in another city) runs ASR → agent → TTS; the agent sees and drives the
phone through **native adb on the Mac**, tunnelled byte-for-byte by the ESP32
(phone hotspot ⇄ ESP32 ⇄ Internet/Cloudflare ⇄ Mac). No app on the phone.

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
| `firmware/extra_components/shuttle/` | Shuttle component: `Kconfig`, `shuttle_config` (settings, NVS provisioning, last phone), `phone_scanner` (finds adbd on the /24, CNXN probe), `adb_bridge` (TCP⇄WS bridge, hello/connect/switch, PSRAM buffers, backoff) | **ours** |
| `firmware/main/boards/shuttle/epaper-1.54-v2/` | board: `shuttle_board.cc` (PTT, battery, bridge, JSON hook), `shuttle_ui.*` (mascot UI, pure LVGL), `shuttle_display.*` (xiaozhi Display → UI mapping, e-paper refresh policy), `mascot_frames.h` (generated), `shuttle_wifi.*` (priority Wi-Fi), `waveshare_bsp.cc` (includes upstream waveshare drivers) | **ours** |
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
| ├ `pipeline/session.py` | `CtrlSession`: xiaozhi protocol, utterance → ASR → (phone commands) → agent → TTS, status pushes | |
| ├ `pipeline/phone_commands.py` | "换手机" / "切换到X" / "哪台手机" / "关闭无线调试" + `/api/phones` actions | |
| ├ `pipeline/{asr,tts,agent}.py` | pluggable interfaces + Qwen/mock ASR, say/openai/command/mock TTS, simple/echo/command/python agents | |
| └ `audio/` | ctypes libopus binding, PCM/WAV helpers | |
| `server/phone_helper/` | optional on-phone helper (Java → dex, run via `app_process`): JPEG screenshots, clipboard for Chinese input | ours |
| `server/tests/` | pytest (fake adb, tunnel, phone, e2e) | ours |
| `tools/sim_device.py` | simulates the ESP32 (ctrl + ADB bridge with phone scan/hello, several `--phone`, mic/WAV/`say`) | ours |
| `tools/fake_adb.py` | fake `adb` binary simulating a phone | ours |
| `tools/update_xiaozhi.sh`, `tools/make_upstream_patch.sh` | upstream sync | ours |
| `deploy/` | `install.sh`, launchd plists (server + watchdog), cloudflared example, `macos-unattended.sh`, `healthcheck.sh` | ours |
| `.github/workflows/` | `firmware.yml` (esp-idf-ci-action v6.1 → merged-binary.bin), `server.yml` (pytest, phone-helper jar) | ours |

## Code conventions

* **xiaozhi code is upstream.** Only 4 upstream files are patched, each block marked `SHUTTLE:`
  (`firmware/UPSTREAM.md`, exact diff in `firmware/upstream.patch`). Put new behaviour in the
  Shuttle component or board directory. If you must touch upstream, keep it tiny, mark it
  `SHUTTLE:`, then run `tools/make_upstream_patch.sh`.
* Upstream rules from `firmware/AGENTS.md` apply: don't block the main loop or audio tasks, change
  device state only via `Application::SetDeviceState`, schedule UI work with `Application::Schedule`,
  keep the board menu in `main/Kconfig.projbuild` sorted (upstream unit test checks it).
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
# --- firmware (needs ESP-IDF v6.1: . ~/esp/esp-idf/export.sh) ---
cd firmware
python build_shuttle.py                         # build (uses shuttle.local.conf), zh-CN
idf.py -p /dev/cu.usbmodem* flash monitor       # flash + serial log (Ctrl-] quits)
idf.py -p PORT monitor                          # log only
idf.py menuconfig                               # Component config → Shuttle (搞快)
python -m unittest discover -s scripts/tests    # upstream build-tooling tests
esptool.py --chip esp32s3 erase_flash           # wipe NVS (Wi-Fi, provisioned URL/token)

# --- server ---
deploy/install.sh                               # first install / update (idempotent)
sudo launchctl kickstart -k system/com.shuttle.server        # restart service
sudo launchctl bootout system/com.shuttle.server             # stop (bootstrap to start again)
tail -f ~/Library/Logs/shuttle/shuttle.log
cd server && .venv/bin/python -m shuttle_server -c config.yaml     # foreground (service stopped)
cd server && .venv/bin/python -m pytest -q      # tests (need libopus: brew install opus)
curl -s localhost:8765/health; curl -s -H "Authorization: Bearer $TOKEN" localhost:8765/status | jq

# --- simulator (no ESP32 needed) ---
python tools/sim_device.py --url ws://127.0.0.1:8765 --token $TOKEN --say "打开设置" --play
python tools/sim_device.py --token $TOKEN --phone <phone-ip>:5555 --bridge-only   # be the ADB bridge
python tools/sim_device.py --token $TOKEN --phone <ip-a> --phone <ip-b> --bridge-only   # two phones
python tools/sim_device.py --token $TOKEN --mic --play                            # PTT with mic (pip install sounddevice)

# --- phone/adb ---
adb devices; adb connect 127.0.0.1:15555; adb -s 127.0.0.1:15555 shell input tap 500 500
curl -H "Authorization: Bearer $TOKEN" localhost:8765/api/phone/screenshot -o s.jpg
curl -H "Authorization: Bearer $TOKEN" localhost:8765/api/phones | jq          # phones found / current
curl -XPOST -H "Authorization: Bearer $TOKEN" localhost:8765/api/phones/next  # switch phone
server/phone_helper/build.sh                    # optional helper jar (needs Android SDK)
```

## Debugging by layer

1. **Wi-Fi** (serial `ShuttleWifi`, `WifiStation`, `ShuttleBoard`): "Network #n" list at boot,
   "Trying higher-priority…", "Wi-Fi '<ssid>' ip=… gw=…, phone: auto(scan)|fixed <ip>". 2.4 GHz
   only. BOOT ×2 = switch to the best network now, BOOT ×3 = portal.
1b. **Phone discovery / choice** (serial `PhoneScanner` "Scan of …/24: N open, M phone(s) [...]",
   `AdbBridge` "Server chose phone …"; server `hello on '<ssid>' … -> host (reason)`, `phone at … is
   <name>`; `GET /api/phones`). No IPs are configured (`PHONE_HOST` empty = scan); the server picks
   by serial (`phones.json`): requested > new phone > pinned > last used > gateway. 🏠 icon = phone
   in use is not the hotspot.
2. **ADB bridge** (serial `AdbBridge`; server `shuttle.adb.tunnel`; `/status` → `tunnels`):
   device states `connecting_server → connecting_phone → active`. HTTP 401 = token mismatch;
   `phone_unreachable` = no `adb tcpip 5555` / client isolation. On the Mac:
   `adb devices` should list `127.0.0.1:15555 device`; `unauthorized` → accept on phone.
   Test the Mac half alone with `sim_device.py --phone <ip>:5555 --bridge-only` from a laptop on
   the phone's hotspot, or the phone half with `adb connect <phone-ip>:5555` directly.
3. **Audio** (serial `Application`, `AudioService`, `WS`; server `shuttle.pipeline.session`):
   "listen start/stop" and "utterance N ms" in the server log; `min_utterance_ms` drops taps.
   Reproduce server-side with `sim_device.py --wav`. Reply audio: `--save-tts out.wav`.
4. **ASR**: log `ASR (qwen/...) N ms: '…'`; swap in `asr.provider: mock` to isolate.
5. **Agent / phone**: log `agent step …`, `tap (x,y) -> device (X,Y)`, `screenshot WxH … via mac|phone`.
   Call the phone API by hand (curl above). `agent.provider: echo` isolates voice from the phone.
6. **TTS**: `TTS failed:` in the log; `tts.provider: mock` produces beeps.
7. **E-paper** (serial `ShuttleDisplay`: "Status: …", "Full refresh …"): every LVGL flush =
   one partial refresh; full refresh every 40 partials / after tasks / every 30 min idle, never
   during a mascot animation. Iterate on layout/art without hardware: `tools/ui_sim/run.sh`
   renders the real `shuttle_ui.cc` to `docs/ui/screens.png`.

## Gotchas

* `build_shuttle.py` regenerates `sdkconfig` from defaults each run — persist settings in
  `shuttle.local.conf`, not via a one-off menuconfig.
* Flash the **merged** image (or `idf.py flash`): the assets partition carries the CJK
  ("common") font; without it most Chinese UI strings render as blanks.
* xiaozhi stores the WebSocket URL/token in NVS; Shuttle rewrites them from Kconfig at every
  boot (`ProvisionXiaozhiSettings`). The OTA reply must keep `"websocket": {}`.
* The ctrl channel is opened on the first BOOT press (TLS takes ~1 s); afterwards the server's
  keepalive keeps it open for `voice.idle_close_s`.
* `adb tcpip 5555` is lost on every phone reboot. `adb -s <ip>:5555 usb` (or the voice command
  "关闭这台手机的无线调试") turns it off without a reboot.
* Switching phones keeps the adb serial `127.0.0.1:15555`; per-phone caches in `PhoneController`
  are dropped via `AdbTunnel.phone_generation`. A new phone shows "Allow USB debugging?" once.
