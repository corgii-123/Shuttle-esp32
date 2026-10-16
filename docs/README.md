# 搞快 Shuttle — setup guide

Voice-control an Android phone with a pocket ESP32. All AI runs on a Mac in another city.

```
                       Mac server (unattended, other city)
                 ┌──────────────────────────────────────────────┐
  Cloudflare ◀───┤ cloudflared ─▶ shuttle-server :8765 (aiohttp) │
  Tunnel         │   /ws/ctrl  xiaozhi voice ─▶ ASR ─▶ Agent ─▶ TTS
  (wss, token)   │   /ws/adb   bytes ◀▶ 127.0.0.1:15555 ◀▶ native adb
                 └──────────────────────────────────────────────┘
        ▲ Internet (through the phone's mobile data)
        │
  ESP32-S3 e-paper (pocket) ── Wi-Fi ── Android phone (hotspot on, adb tcpip 5555)
    BOOT = push-to-talk, ADB bridge  TCP gateway:5555 ⇄ /ws/adb
```

Contents: [1 Phone](#1-phone) · [2 Mac server](#2-mac-server) · [3 Cloudflare Tunnel](#3-cloudflare-tunnel-fixed-hostname) ·
[4 Tailscale SSH](#4-tailscale-ssh-remote-maintenance) · [5 Unattended Mac](#5-unattended-mac) ·
[6 Firmware](#6-firmware) · [7 Testing without hardware](#7-testing-without-hardware) ·
[8 Troubleshooting](#8-troubleshooting). Protocol details: [protocol.md](protocol.md).
Bring-up checklist and open risks: [../HANDOFF.md](../HANDOFF.md).

---

## 1. Phone

1. Settings → About → tap *Build number* 7× → Developer options → **USB debugging ON**.
   (Xiaomi/MIUI/HyperOS: also *USB debugging (Security settings)* to allow input injection.)
2. Hotspot: **2.4 GHz band** (the ESP32 has no 5 GHz), WPA2, note SSID/password.
   Some phones turn the hotspot off when idle — disable that timeout.
3. With USB to any computer that has adb:
   ```bash
   adb devices            # accept the RSA prompt on the phone
   adb tcpip 5555         # adbd now also listens on TCP 5555 (until the phone reboots!)
   ```
   **Repeat `adb tcpip 5555` after every phone reboot** (by design, manual step).
4. The first time the Mac connects through the tunnel the phone asks *"Allow USB debugging?"*
   for the Mac's key → tick *Always allow* → Allow. The e-paper shows "请在手机上授权" meanwhile.

No app is installed. Optional: [ADBKeyboard](https://github.com/senzhk/ADBKeyBoard) as a fallback
for Chinese input if the clipboard method is blocked by your ROM.

## 2. Mac server

```bash
git clone <this repo> ~/shuttle && cd ~/shuttle
deploy/install.sh            # brew deps, venv, config.yaml with a random token, LaunchDaemon + watchdog
```

`install.sh` prints the generated token — it goes into the firmware (`CONFIG_SHUTTLE_TOKEN`).
Edit `server/config.yaml` (all options documented in `server/config.example.yaml`), then
`sudo launchctl kickstart -k system/com.shuttle.server`.

| Piece | Default | Change to |
|---|---|---|
| ASR | `qwen`: OpenAI-compatible endpoint `http://127.0.0.1:8000/v1`, `api: transcriptions` | your Qwen ASR server (`api: chat` for `/chat/completions` + `audio_url`). Until it runs: `asr.provider: mock` |
| Agent | `simple` (rule-based demo of the screenshot → decide → act loop) | your pi agent: `agent.provider: command` (process using the HTTP phone API) or `python` (class implementing `Agent`), see §2.1 |
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
  process gets `SHUTTLE_PHONE_API` (`http://127.0.0.1:8765/api/phone?device=…`) and
  `SHUTTLE_PHONE_TOKEN`; it drives the phone via [the phone API](protocol.md#4-apiphone--phone-api-for-external-agents),
  prints `STATUS: <text>` lines on stderr for progress and the final reply as the last stdout line.

### 2.2 Qwen ASR

Any OpenAI-compatible server works, e.g. on a CUDA box `vllm serve Qwen/Qwen3-ASR-1.7B --port 8000`
(`api: transcriptions` or `chat`). On Apple Silicon use whatever Qwen-ASR runtime you have that
exposes `/v1/audio/transcriptions` (MLX ports exist); only `asr.qwen.base_url/api/model` change.
The client strips Qwen3-ASR's `language Chinese<asr_text>` prefix.

## 3. Cloudflare Tunnel (fixed hostname)

Gives the ESP32 a stable `wss://shuttle.example.com` without opening ports on the Mac.

```bash
brew install cloudflared
cloudflared tunnel login                       # pick the zone (domain) in the browser
cloudflared tunnel create shuttle              # note the UUID; creds in ~/.cloudflared/<UUID>.json
cloudflared tunnel route dns shuttle shuttle.example.com
cp deploy/cloudflared-config.example.yml ~/.cloudflared/config.yml   # fill UUID, user, hostname
cloudflared tunnel --config ~/.cloudflared/config.yml run            # test in foreground
sudo cloudflared --config ~/.cloudflared/config.yml service install  # launchd, starts at boot
curl https://shuttle.example.com/health
```

* WebSockets work out of the box; keepalives (server pings 20–30 s) stay below Cloudflare's idle timeout.
* Leave Cloudflare Access **off** for this hostname (the ESP32 cannot do the Access login) —
  the token protects the WebSockets; `/health` and `/xiaozhi/ota/` reveal no secrets.
* Optional hardening: a WAF rule allowing only `/ws/*`, `/health`, `/xiaozhi/ota/`, `/firmware/*`.

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
brew install cmake ninja dfu-util ccache python@3.12
mkdir -p ~/esp && cd ~/esp
git clone -b v6.1 --recursive --shallow-submodules --depth 1 https://github.com/espressif/esp-idf.git
cd esp-idf && ./install.sh esp32s3
. ~/esp/esp-idf/export.sh            # every new shell (alias get_idf='. ~/esp/esp-idf/export.sh')
```

### 6.2 Configure, build, flash

```bash
cd firmware
cp shuttle.local.conf.example shuttle.local.conf    # server URL, token, Wi-Fi list (git-ignored)
python build_shuttle.py                              # = scripts/build.py shuttle/epaper-1.54-v2 ... --language zh-CN
idf.py -p /dev/cu.usbmodem* flash monitor            # or: esptool.py write_flash 0x0 build/merged-binary.bin
```

* Settings can also be changed with `idf.py menuconfig` → *Component config → Shuttle (搞快)*
  (after one `build_shuttle.py` run). `build_shuttle.py` regenerates sdkconfig each time, so keep
  durable values in `shuttle.local.conf`.
* If the port does not show up: hold **BOOT**, tap **PWR**/reset, release BOOT (download mode).
* CI: GitHub Actions *Firmware* builds the same image; artifact `shuttle-firmware-<sha>`
  (`merged-binary.bin` at 0x0). Add repository secrets `SHUTTLE_SERVER_URL`, `SHUTTLE_TOKEN`,
  `SHUTTLE_WIFI1_SSID/PASSWORD`… to get a ready-to-flash image (artifacts are visible to repo readers).
* Wi-Fi can also be added at runtime: press BOOT 3× → AP `Shuttle-XXXX` → http://192.168.4.1.
  Kconfig networks have priority over portal ones.

### 6.3 Serial log

`idf.py monitor` (Ctrl-] to quit). Shuttle tags: `ShuttleBoard`, `ShuttleWifi`, `ShuttleConfig`,
`AdbBridge`, `ShuttleDisplay`; xiaozhi: `Application`, `WS`, `WifiStation`, `Ota`.

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
exactly the ESP32's job (ADB bridge + push-to-talk), so the whole chain is exercised.

## 8. Troubleshooting

| Symptom | Look at | Usual cause |
|---|---|---|
| e-paper "服务器不可达" | serial `AdbBridge` errors, `curl https://<host>/health` | wrong URL/token (HTTP 401 in serial log), tunnel down |
| "手机ADB未开启" | serial `Phone ... unreachable` | `adb tcpip 5555` not done since phone reboot; hotspot client isolation |
| "请在手机上授权" | `adb devices` on the Mac shows `unauthorized` | accept the RSA prompt on the phone |
| "本网络无手机" | – | on home Wi-Fi with `PHONE_HOST="none"` |
| voice: nothing recognised | `shuttle.log` ASR lines, `/status` last_text | ASR endpoint down → try `asr.provider: mock` |
| no sound | serial `Application` / `AudioService` | TTS failed (`shuttle.log`), volume 0 |
| taps land in the wrong place | log line `tap (x,y) -> device (X,Y)` | agent used device coordinates; PhoneController expects screenshot pixels |
| Chinese text not typed | log `clipboard:` errors | ROM blocks shell clipboard → install ADBKeyboard or `input_method: adbkeyboard` |
