# 搞快 Shuttle — setup guide

Voice-control an Android phone with a pocket ESP32. All AI runs on a Mac in another city.

```
                         Mac server (unattended, other city)
                 ┌──────────────────────────────────────────────┐
  Cloudflare ◀───┤ cloudflared ─▶ shuttle-server :8780 (aiohttp) │
  Tunnel         │   /ws/ctrl  xiaozhi voice ─▶ ASR ─▶ Agent ─▶ TTS
  (wss, token)   │   /ws/adb   bytes ◀▶ 127.0.0.1:15555 ◀▶ native adb
                 └──────────────────────────────────────────────┘
        ▲ Internet
        │
  At home (router)                               Out (hotspot)
  router ── Wi-Fi ── ESP32                       phone hotspot ── Wi-Fi ── ESP32
     ├──── Wi-Fi ── phone A (:5555)              (phone = gateway, :5555, mobile data)
     └──── Wi-Fi ── phone B (:5555)
```

The ESP32 only needs *some* 2.4 GHz Wi-Fi that reaches the Internet **and** a phone's adbd.
**No phone IP is configured anywhere**: on every network the board scans for phones that ran
`adb tcpip 5555` (the hotspot phone, or any number of phones on the router) and the Mac picks
one, recognising phones by serial number. Networks are tried in priority order; after coming
home the device moves from the hotspot to home Wi-Fi by itself (or immediately on
**BOOT double-click**).

Contents: [1 Phone](#1-phone) · [2 Mac server](#2-mac-server) · [3 Cloudflare Tunnel](#3-cloudflare-tunnel-fixed-hostname) ·
[4 Tailscale SSH](#4-tailscale-ssh-remote-maintenance) · [5 Unattended Mac](#5-unattended-mac) ·
[6 Firmware](#6-firmware) · [7 Testing without hardware](#7-testing-without-hardware) ·
[8 Troubleshooting](#8-troubleshooting). Protocol details: [protocol.md](protocol.md).
Bring-up checklist and open risks: [../HANDOFF.md](../HANDOFF.md).

---

## 1. Phone

1. Settings → About → tap *Build number* 7× → Developer options → **USB debugging ON**.
   (Xiaomi/MIUI/HyperOS: also *USB debugging (Security settings)* to allow input injection.)
2. With USB to any computer that has adb:
   ```bash
   adb devices            # accept the RSA prompt on the phone
   adb tcpip 5555         # adbd now also listens on TCP 5555, on every network (until reboot!)
   ```
   **Repeat `adb tcpip 5555` after every phone reboot** (by design, manual step).
3. The first time the Mac connects through the tunnel the phone asks *"Allow USB debugging?"*
   for the Mac's key → tick *Always allow* → Allow. The e-paper shows "请手机授权" meanwhile.

No app is installed. Optional: [ADBKeyboard](https://github.com/senzhk/ADBKeyBoard) as a fallback
for Chinese input if the clipboard method is blocked by your ROM.

### 1.1 Home / office Wi-Fi

```
Mac ⇄ Internet ⇄ home router ⇄ Wi-Fi ⇄ ESP32
                      ⇅ Wi-Fi
                phone(s) with adb tcpip 5555
```

1. Phone and ESP32 on the **same LAN** with no client isolation: don't use a *guest* network or
   "AP isolation". Dual-band routers with one SSID are fine (the phone may sit on 5 GHz, the
   ESP32 on 2.4 GHz — same LAN).
2. Nothing else: no fixed IP, no DHCP reservation. The board finds the phone (e-paper:
   **搜索手机** → phone name). If the DHCP server gives the phone another IP tomorrow, the next
   scan finds it again and the Mac still knows it is the same phone.
3. Check from any computer on the same Wi-Fi: `adb connect <phone-ip>:5555`.

The phone must be **connected to this Wi-Fi** (not only running its hotspot). Many phones turn
Wi-Fi client off while the hotspot is on — switch the hotspot off at home. No phone found →
e-paper says **未发现手机**.

### 1.2 Hotspot (out)

1. Hotspot on the **2.4 GHz band** (the ESP32 has no 5 GHz), WPA2. Disable "turn off hotspot
   when idle".
2. Nothing to configure: the hotspot phone is the gateway, the scan checks it first. Not found →
   **未发现手机**: `adb tcpip 5555` was lost (phone rebooted).

### 1.3 Several phones

Every phone that ran `adb tcpip 5555` and is on the board's Wi-Fi is a candidate (the e-paper
shows e.g. `Pixel 8 +1` = connected to Pixel 8, one more phone around). Which one is used:

| Situation | Result |
|---|---|
| a phone that was **never used before** runs `adb tcpip 5555` | the board finds it within a minute (`CONFIG_SHUTTLE_PHONE_SCAN_INTERVAL_S`) and **switches to it** (when no task runs). That's the natural way to "move" the board to another phone |
| a phone used before comes back | nothing changes — the current phone stays |
| say **"换手机"** / **"下一台手机"** | next phone |
| say **"切换到小米"** (any part of its name / model / alias) | that phone, and it stays the preferred one |
| say **"现在连的是哪台手机"** | the board tells you (and lists the others) |
| the current phone disappears (left, rebooted) | the board rescans and takes the next one |

Two phones that both ran `adb tcpip 5555` don't conflict: each listens on its own IP. To make a
phone stop listening (rarely needed — just switch): `adb -s <ip>:5555 usb` from a computer on the
same Wi-Fi, toggle USB debugging, or reboot it. There is deliberately no voice command for this:
turning it back on needs a USB cable (`adb tcpip 5555`), so it cannot be undone remotely. Names: the phone's *Device name* (Settings → About phone), model, or
`phones.aliases` in `server/config.yaml`. HTTP: `GET /api/phones`, `POST /api/phones/{select,next,rescan}`
(see [protocol.md](protocol.md#several-phones--apiphones)).

Optional: `CONFIG_SHUTTLE_WIFIn_PHONE_HOST="<ip>"` pins a network to one phone (no scan),
`"none"` disables ADB on it.

### 1.4 Switching Wi-Fi

| Situation | What happens |
|---|---|
| Boot | tries Wi-Fi #1 (home) for 20 s, then every configured network by signal strength |
| Leave home | home Wi-Fi drops → the device rescans and joins the hotspot (seconds once it is on) |
| Come home (still on hotspot) | while idle it checks for home Wi-Fi after 5 min, then 10, 20 … up to 60 min (each check ≈20 s offline) |
| **BOOT double-click** | check for a better network **now** (or rescan if offline) |
| BOOT ×3 | Wi-Fi portal (AP `Shuttle-XXXX`, http://192.168.4.1) to add a network on the spot; portal networks rank below Kconfig ones (phones found automatically) |

The status bar shows a 🏠 icon when the phone in use is not the hotspot (reached via a router).
ADB keeps working across switches (adbd listens on all interfaces); the Mac's adb sees a short
disconnect and reconnects.

## 2. Mac server

```bash
git clone <this repo> ~/shuttle && cd ~/shuttle
deploy/install.sh            # brew deps, venv, config.yaml with a random token, LaunchDaemon + watchdog
```

`install.sh` prints the generated token — it goes into the firmware (`CONFIG_SHUTTLE_TOKEN`).
Edit `server/config.yaml` (all options documented in `server/config.example.yaml`), then
`sudo launchctl kickstart -k system/com.shuttle.server` (installed with `--agent`:
`launchctl kickstart -k gui/$(id -u)/com.shuttle.server`).

| Piece | Default | Change to |
|---|---|---|
| ASR | `qwen`: OpenAI-compatible `http://127.0.0.1:8765/v1` (`mlx_audio.server`, model `mlx-community/Qwen3-ASR-1.7B-8bit`), `api: transcriptions` | any Qwen ASR server (`api: chat` for `/chat/completions` + `audio_url`). Until it runs: `asr.provider: mock` |
| Agent | `llm`: vision LLM with tool calling at `http://127.0.0.1:8000/v1` (Splash, Qwen3.8-27B) — see §2.2 | `simple` (rule-based demo), or your own agent: `command` (process using the HTTP phone API) / `python` (class implementing `Agent`), see §2.1 |
| TTS | `say` (macOS, voice `Tingting`) | `openai` (any `/audio/speech`), `command`, `mock` |

Logs: `~/Library/Logs/shuttle/shuttle.log` (rotated 10 × 10 MB). Health:
`deploy/healthcheck.sh` locally, `deploy/healthcheck.sh https://shuttle.example.com` from anywhere.
`/status` (with token) shows tunnels, sessions, last command/reply and the redacted config.

Manual run (debugging): `cd server && .venv/bin/python -m shuttle_server -c config.yaml`
(stop the service first: `sudo launchctl bootout system/com.shuttle.server`).

### 2.1 Plugging in your agent (pi)

The contract (`server/shuttle_server/pipeline/agent.py`):

```python
class Agent(abc.ABC):
    async def run(self, instruction: str, ctx: AgentContext) -> str: ...
# ctx.phone   : PhoneController (screenshot(), tap(x,y), swipe(...), input_text(...), back(), home(),
#               dump_ui(), ui_elements(), launch_app(pkg), ...)  coordinates = screenshot pixels
# ctx.report  : await ctx.report("acting", "点击 搜索")   -> shown on the e-paper
# ctx.cancelled, ctx.max_steps, ctx.step_delay_s
# return      : the sentence spoken back to the user
```

* **In-process** (`agent.provider: python`, `agent.python.target: "my_pi_agent:PiAgent"`): put
  `my_pi_agent.py` on `PYTHONPATH` (e.g. in `server/`), subclass `Agent`.
* **Out-of-process** (`agent.provider: command`, `argv: ["pi", "-p", "{instruction}"]`): the
  process gets `SHUTTLE_PHONE_API` (`http://127.0.0.1:8780/api/phone?device=…`) and
  `SHUTTLE_PHONE_TOKEN`; it drives the phone via [the phone API](protocol.md#4-apiphone--phone-api-for-external-agents),
  prints `STATUS: <text>` lines on stderr for progress and the final reply as the last stdout line.

### 2.2 Local models (the dev Mac)

| Port | Service (LaunchAgent) | Model | Used for |
|---|---|---|---|
| 8000 | Splash `com.local.splash-qwen38` | `unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M` — **vision**, tool calling | `agent.llm` |
| 8001 | Splash `com.local.splash-qwen38-heretic` | `mradermacher/Qwen3.8-27B-heretic-ara-i1-GGUF:Q4_K_M` — **text only** | not used (the agent needs screenshots; `vision: auto` detects text-only models via `GET /models` and falls back to the uiautomator element list) |
| 8765 | mlx-audio `com.local.qwen3-asr` | `mlx-community/Qwen3-ASR-1.7B-8bit` | `asr.qwen` (~0.15 s per command) |
| 8780 | Shuttle | — | ESP32 `ws://…:8780` |

`LlmAgent` (`server/shuttle_server/pipeline/llm_agent.py`): per step it sends the task, a short
history, the foreground app and **one** screenshot (720 px short side, ~1150 tokens; older
screenshots are dropped) and gets exactly one tool call back: `tap/long_press/swipe` (0..1000
grid, Qwen-VL convention), `scroll`, `type_text` (Chinese via the phone helper's clipboard),
`launch_app` (spoken name → installed package, `pipeline/apps.py`), `back`, `home`, `wait`,
`finish(reply)`. A bare "打开<应用>" skips the LLM (`fast_launch`). ~2.8 s per step on an M5 Max;
the system prompt is kept stable so Splash's prefix cache hits. It stops after
`agent.max_steps`, 3 consecutive failed actions, a user abort (touch) or a new utterance.

Any OpenAI-compatible ASR works too, e.g. on a CUDA box `vllm serve Qwen/Qwen3-ASR-1.7B`
(`api: transcriptions` or `chat`). The client strips Qwen3-ASR's `language Chinese<asr_text>` prefix.

## 3. Cloudflare Tunnel (fixed hostname)

Gives the ESP32 a stable `wss://shuttle.example.com` without opening ports on the Mac.
Only the Shuttle server (port 8780) goes through it; the model servers stay on 127.0.0.1.

**Tunnel created in the Cloudflare dashboard** (remotely managed: you get a token, the route
`shuttle.example.com → http://localhost:8780` is configured there):

```bash
brew install cloudflared
cloudflared service install "$(cat ~/Downloads/.cf-tunnel-token)"   # LaunchAgent com.cloudflare.cloudflared
chmod 600 ~/Library/LaunchAgents/com.cloudflare.cloudflared.plist      # it contains the token
grep "Registered tunnel connection" ~/Library/Logs/com.cloudflare.cloudflared.err.log
curl https://shuttle.example.com/health                              # {"status": "ok"}
```

Without `sudo` this is a login service like the Shuttle server and the local models (with
`sudo` it would start at boot, but nothing behind it would). The token is the key of the tunnel:
whoever has it can run a connector for your hostname; delete the downloaded copy, rotate it in
the dashboard if it leaks.

**Or locally managed** (`cloudflared tunnel login` / `create` / `route dns`, config file
`deploy/cloudflared-config.example.yml`, `sudo cloudflared --config … service install`).

Security (the hostname is public):

* Shuttle checks the 48-character token on `/ws/ctrl` and `/ws/adb`. `/status` and the
  phone-control API `/api/*` answer only programs on this Mac (`server.remote_api: false`):
  through the tunnel (Cloudflare adds `CF-Ray`) they return 404 even with the token, so a token
  read out of a lost board cannot screenshot or drive the phone. `/health` tells outsiders only
  `{"status": "ok"}`; 20 bad tokens from one address in 10 min block it for 15 min.
* Leave Cloudflare Access, Bot Fight Mode, "Under attack" and any challenge **off** for this
  hostname: the ESP32 cannot solve them.
* WAF custom rule (recommended): block everything on the hostname except `/ws/ctrl`, `/ws/adb`,
  `/xiaozhi/ota` (and `/xiaozhi/ota/`), `/health`. Rate limit, e.g. 30 requests / 10 s per IP.
* WebSockets work out of the box; keepalives (server pings 20–30 s) stay below Cloudflare's idle timeout.

From mainland China the direct route to Cloudflare is poor in the evening (measured: 300 ms,
10 % loss from home broadband; the board then moved ~0.3 KB/s). Where the Mac is on the same
LAN, give that Wi-Fi a direct server address (`CONFIG_SHUTTLE_WIFI<n>_SERVER_URL=
"ws://<mac-ip>:8780"`, server `host: 0.0.0.0`); the tunnel is then only used elsewhere. Outside
mainland China (e.g. Taiwan: Taipei PoP) the tunnel is fast.

## 4. Tailscale SSH (remote maintenance)

```bash
brew install --cask tailscale          # or the App Store version; log in
sudo tailscale up --ssh                # standalone/open-source tailscaled supports --ssh
# from your laptop (same tailnet):
ssh <user>@<mac-name>                  # or: tailscale ssh <user>@<mac-name>
```

The GUI (App Store) variant cannot be a Tailscale SSH *server*; then enable macOS
*Remote Login* (System Settings → General → Sharing) and ssh over the tailnet IP. Make sure
Tailscale starts at boot (run `tailscaled` as a LaunchDaemon: `sudo tailscaled install-system-daemon`
for the open-source build).

Typical remote session: `ssh mac`, `cd ~/shuttle && git pull && deploy/install.sh --no-brew`,
`tail -f ~/Library/Logs/shuttle/shuttle.log`, `claude` (Claude Code) for debugging.

## 5. Unattended Mac

```bash
deploy/macos-unattended.sh     # pmset: never sleep, autorestart after power loss, wake on LAN, restart on freeze
```
Also (GUI): Energy → *Start up automatically after a power failure*, *Prevent automatic sleeping*;
disable automatic macOS updates; **FileVault blocks unattended reboots** (nothing starts until
someone unlocks) — disable it or use `sudo fdesetup authrestart` for planned restarts.
The server is a LaunchDaemon (starts at boot, no login needed); `KeepAlive` restarts it on crash,
the watchdog job restarts it if `/health` fails 3× in a row (5 min interval).

## 6. Firmware

### 6.1 ESP-IDF v6.1 on macOS

```bash
brew install cmake ninja dfu-util ccache
mkdir -p ~/esp && cd ~/esp
git clone -b v6.1 --recursive --shallow-submodules --depth 1 https://github.com/espressif/esp-idf.git
# ESP-IDF v6.1 accepts Python 3.10-3.13. If `python3` is newer (Homebrew 3.15 on the dev Mac):
mkdir -p ~/esp/pybin && ln -sf "$(uv python find 3.12)" ~/esp/pybin/python3 && ln -sf ~/esp/pybin/python3 ~/esp/pybin/python
export PATH=~/esp/pybin:$PATH
cd esp-idf && ./install.sh esp32s3
. ~/esp/esp-idf/export.sh            # every new shell (with ~/esp/pybin first in PATH)
```

### 6.2 Configure, build, flash

```bash
cd firmware
cp shuttle.local.conf.example shuttle.local.conf    # server URL, token, Wi-Fi list (git-ignored)
python build_shuttle.py                              # = scripts/build.py shuttle/epaper-1.54-v2 ... --language zh-CN --wake-word disabled
../tools/flash_shuttle.sh                            # flash + boot (watchdog reset, see 6.3)
python ../tools/esp_console.py                       # serial console that does not reset the board
```

* Settings can also be changed with `idf.py menuconfig` → *Component config → Shuttle (搞快)*
  (after one `build_shuttle.py` run). `build_shuttle.py` regenerates sdkconfig each time, so keep
  durable values in `shuttle.local.conf`.
* First flash over the factory firmware (it does not answer esptool): power off (long PWR),
  hold **BOOT** (light-bulb key), plug in USB / press PWR, release BOOT → download mode.
  Back up the factory image first: `esptool --before no-reset read-flash 0 0x800000 factory.bin`.
* CI: GitHub Actions *Firmware* builds the same image; artifact `shuttle-firmware-<sha>`
  (`merged-binary.bin` at 0x0). Add repository secrets `SHUTTLE_SERVER_URL`, `SHUTTLE_TOKEN`,
  `SHUTTLE_WIFI1_SSID/PASSWORD`… to get a ready-to-flash image (artifacts are visible to repo readers).
* Wi-Fi can also be added at runtime: **double-click PWR** → AP `Shuttle-XXXX` →
  http://192.168.4.1. Kconfig networks have priority over portal ones; phones are found automatically on them
  too. Saved networks are
  remembered (NVS), no re-flash needed. The ESP32 is 2.4 GHz only.
* Keys and touch: hold BOOT = talk (recording starts on press and continues
  `CONFIG_SHUTTLE_PTT_TAIL_MS` after release); PWR click = switch to the best Wi-Fi now;
  PWR ×2 = Wi-Fi portal; long PWR = power off; tap the screen while busy = cancel, while idle =
  device details; long press on the screen = ghost-clearing full refresh.

### 6.3 Serial log

`python tools/esp_console.py` (`--reboot` restarts the board first, `--log file -q` for a
background logger). Not `idf.py monitor` on macOS: opening the USB-Serial/JTAG port raises
DTR+RTS together, which the ESP32-S3 treats as "reset into download mode" (the board then
sits at "waiting for download"). Shuttle tags: `ShuttleBoard`, `ShuttleWifi`, `ShuttleConfig`,
`AdbBridge`, `PhoneScanner`, `ShuttleDisplay`, `ShuttleTouch`, `DebugConsole`; xiaozhi:
`Application`, `AudioService`, `WS`, `WifiStation`, `Ota`. The console is the USB-Serial/JTAG
port only (board `config.json`): upstream's default also mirrors every line to UART0 at
115200 baud, which throttled all logging to ~11 KB/s.

Debug console (`CONFIG_SHUTTLE_DEBUG_CONSOLE=y` in `shuttle.local.conf`, off by default):
drive the board from the computer it is plugged into, e.g. for audio tests.

```bash
python tools/esp_console.py --log ~/esp/serial.log -q --fifo ~/esp/serial.in &
echo "ptt 3000" > ~/esp/serial.in    # hold the light-bulb key 3 s (say something near the board)
echo "stats 1500" > ~/esp/serial.in  # CPU per task over 1.5 s (send right after "ptt")
# also: tap | hold (touch gestures), pwr (PWR click), dump (screen), reboot
```

With `voice.save_utterances_dir` the server keeps every utterance as WAV; its log line
`utterance N ms: P packets in T s (first after F s)` should show N ≈ T − F (no lost frames)
and F ≈ 0.1–0.3 s.

`dump` logs the current e-paper frame; with `CONFIG_SHUTTLE_DEBUG_SCREEN_DUMP=y` every frame is
logged. `python tools/screen_dump.py serial.log --all -o frames/` renders them as PNG.

## 7. Testing without hardware

```bash
cd server && python -m venv .venv && .venv/bin/pip install -r requirements.txt   # + brew install opus
.venv/bin/python -m pytest -q                       # mock adb (tools/fake_adb.py), tunnel, pipeline, e2e
```

End-to-end with the simulator (fake phone, mock ASR/TTS):

```bash
cat > /tmp/sim.yaml <<EOF
auth: {token: devtoken}
logging: {dir: "", console: true}
adb: {path: "$(pwd)/.venv/bin/python $(pwd)/../tools/fake_adb.py", fallback_serial: fake-phone}
asr: {provider: mock, mock: {text: "打开设置，然后点击微信"}}
tts: {provider: mock}
EOF
.venv/bin/python -m shuttle_server -c /tmp/sim.yaml &
.venv/bin/python ../tools/sim_device.py --token devtoken --save-tts /tmp/reply.wav
```

With real pieces: `asr.provider: qwen`, `tts.provider: say`, a real phone on the same Wi-Fi
(`adb tcpip 5555`) and `sim_device.py --phone <phone-ip>:5555 --say "打开设置" --play` — this is
exactly the ESP32's job (ADB bridge + push-to-talk), so the whole chain is exercised. Several
`--phone` options simulate several phones on the Wi-Fi (the server picks; try "换手机").

## 8. Troubleshooting

| Symptom | Look at | Usual cause |
|---|---|---|
| e-paper "服务器断开" (☁̸ icon) | serial `AdbBridge` errors, `curl https://<host>/health` | wrong URL/token (HTTP 401 in serial log), tunnel down |
| "未发现手机" | serial `PhoneScanner: Scan of …: N open, 0 phone(s)` | `adb tcpip 5555` not done since phone reboot; phone on another Wi-Fi (hotspot still on?), guest network / AP isolation; a sleeping phone answered too late → raise `CONFIG_SHUTTLE_PHONE_SCAN_TIMEOUT_MS` |
| wrong phone used | `GET /api/phones`, server log `hello on … -> host (reason)` | say "切换到<名字>" (pins it) or `POST /api/phones/select`; `phones.auto_switch_new: false` stops auto-switching to newly found phones |
| "请手机授权" | `adb devices` on the Mac shows `unauthorized` | accept the RSA prompt on the phone |
| "连不上手机" | `adb connect <phone-ip>:5555` from a laptop on the same Wi-Fi | the phone just left / rebooted — the board rescans and moves on |
| "未配置手机" | – | network with `PHONE_HOST="none"` |
| voice: nothing recognised | `shuttle.log` ASR lines, `/status` last_text | ASR endpoint down → try `asr.provider: mock` |
| no sound | serial `Application` / `AudioService` | TTS failed (`shuttle.log`), volume 0 |
| taps land in the wrong place | log line `tap (x,y) -> device (X,Y)` | agent used device coordinates; PhoneController expects screenshot pixels |
| Chinese text not typed | log `clipboard:` errors | ROM blocks shell clipboard → install ADBKeyboard or `input_method: adbkeyboard` |
