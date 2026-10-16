# Shuttle protocol

All traffic goes **ESP32 → (phone hotspot) → Internet → Cloudflare Tunnel → Mac server**, on one
HTTPS hostname (e.g. `shuttle.example.com`, local port 8765). The ESP32 always connects out;
nothing on the device or the phone listens to the Internet.

| Path | Kind | Auth | Purpose |
|---|---|---|---|
| `wss://<host>/ws/ctrl` | WebSocket | token | xiaozhi-compatible voice + control |
| `wss://<host>/ws/adb` | WebSocket | token | ADB byte pass-through (ESP32 ⇄ phone adbd) |
| `https://<host>/xiaozhi/ota/` | HTTP POST/GET | none (no secrets returned) | xiaozhi boot-time OTA/config check |
| `https://<host>/health` | HTTP GET | none | liveness (no secrets) |
| `https://<host>/status` | HTTP GET | token | detailed state |
| `https://<host>/api/phone/<action>` | HTTP | token | phone control API for external agents |
| `https://<host>/firmware/<file>` | HTTP GET | none | OTA firmware download (only if `ota.firmware_dir`) |

## Authentication

Every WebSocket handshake (and `/status`, `/api/phone/*`) must carry the pre-shared token:

```
Authorization: Bearer <token>
```

(`X-Shuttle-Token: <token>` is also accepted; `?token=` only if `auth.allow_query_token`.)
A missing/wrong token is rejected with **HTTP 401 before the upgrade**. The firmware takes
the token from `CONFIG_SHUTTLE_TOKEN`; the server from `config.yaml → auth.token`.

Other headers sent by the device (both channels):

| Header | Value |
|---|---|
| `Device-Id` | Wi-Fi MAC (`aa:bb:cc:dd:ee:ff`) — pairs the ctrl and adb channels of one device |
| `Client-Id` | UUID (ctrl only, xiaozhi) |
| `Protocol-Version` | `1` (ctrl only; 2/3 = xiaozhi binary framing, also supported) |
| `X-Shuttle-Channel` | `adb` (adb only, informational) |

## Heartbeat & reconnect

| | ESP32 | Server |
|---|---|---|
| `/ws/adb` | WS ping every 10 s, reconnect if no pong in 30 s (`CONFIG_SHUTTLE_ADB_PING_INTERVAL_S` / `..._PONG_TIMEOUT_S`); TCP keepalive to the phone (10 s idle, 5 s × 3); exponential backoff 1 s → 30 s with ±20 % jitter (phone failures start at 3 s); reset after a session lasting ≥ 10 s | WS ping every 20 s (aiohttp heartbeat) |
| `/ws/ctrl` | xiaozhi: opened on first button press, considered dead after 120 s without incoming data; re-opened on next press | WS ping every 30 s **plus** `{"type":"shuttle","event":"ping"}` every `voice.keepalive_s` (30 s) — the JSON keeps xiaozhi's 120 s timeout from firing; closes after `voice.idle_close_s` (600 s) idle to save battery |
| Wi-Fi | esp-wifi-connect rescans every 5…30 s; Shuttle retries higher-priority SSIDs every `CONFIG_SHUTTLE_WIFI_ROAM_INTERVAL_S` while idle | – |

---

## 1. `/ws/adb` — byte pass-through

The ESP32 does **not** implement ADB. It splices one TCP connection to `<phone>:5555` with
one WebSocket. The Mac's native `adb` talks to the phone through this "cable".

```
Mac adb server ──TCP──▶ 127.0.0.1:15555 ═══(server)═══ WS /ws/adb ═══ ESP32 ──TCP──▶ phone adbd :5555
                         adb connect 127.0.0.1:15555                    phone = DHCP gateway of the hotspot
```

### Frames

* **Binary frames: raw ADB bytes**, both directions, arbitrary chunking (message boundaries
  carry no meaning). This is the only data path.
* Text frames: optional small JSON control messages; receivers that do not understand them
  ignore them.
  * device → server: `{"type":"phone","state":"connected","detail":"192.168.43.1:5555"}` once the
    phone TCP connection is up; `{"type":"phone","state":"unreachable","detail":"..."}` right
    before closing with code 4001.

### Session lifecycle (one WebSocket = one phone TCP connection = one ADB transport)

1. Device has an IP → resolves the phone host (gateway, or `CONFIG_SHUTTLE_WIFIn_PHONE_HOST`).
2. Device opens `/ws/adb`. From now on server bytes are buffered in PSRAM
   (`CONFIG_SHUTTLE_ADB_RX_BUFFER_KB`, default 256 KB).
3. Device connects TCP to `phone:5555` (5 s timeout).
   * fails → text `phone/unreachable`, close **4001**, back off, go to 2.
   * ok → text `phone/connected`, start pumping both ways.
4. Server, on a new `/ws/adb` session: opens the local listener (`adb.local_port`, per
   Device-Id) if not open, and its supervisor runs `adb connect 127.0.0.1:<port>` whenever adb
   does not list the serial as `device`.
5. When adb connects to the local port, the server pairs that TCP connection with the newest
   **unused, phone-ready** session (ready = `phone/connected` received, or 3 s passed). Bytes
   flow until either side closes.
6. **Either side closes → the other side is closed too.** Sessions are single-use: the device
   reconnects (≈0.3 s after a long session, back-off after short ones) and the next adb TCP
   connection gets a fresh phone TCP connection (fresh ADB handshake).
7. A new adb connection while a session is paired means adb abandoned the old transport: the
   server closes the old session (code 4002).
8. If adb reports `offline` for `adb.offline_reset_s` while paired, the server runs
   `adb disconnect` and closes the session (4003).
9. Device gone for `adb.device_grace_s` (60 s) → listener closed, `adb disconnect 127.0.0.1:<port>`.

Close codes: 1000 normal, 4000 replaced by a newer session, 4001 phone unreachable,
4002 adb reconnected, 4003 adb offline reset.

Authorization dialog: the first `adb connect` makes the phone show "Allow USB debugging?" for the
Mac's adb key. Until accepted, `adb devices` shows `unauthorized` (also pushed to the e-paper).

---

## 2. `/ws/ctrl` — xiaozhi-compatible voice & control

Compatible with xiaozhi-esp32's WebSocket protocol
([upstream doc](../firmware/docs/websocket.md)); a stock xiaozhi device pointed at this server
works too (using auto-mode VAD). Shuttle uses **manual (push-to-talk) mode**.

### Handshake

device → server
```json
{"type":"hello","version":1,"features":{"mcp":true},"transport":"websocket",
 "audio_params":{"format":"opus","sample_rate":16000,"channels":1,"frame_duration":60}}
```
server → device (within 10 s, otherwise the device gives up)
```json
{"type":"hello","transport":"websocket","session_id":"3f2a9c...",
 "audio_params":{"format":"opus","sample_rate":24000,"channels":1,"frame_duration":60}}
```
followed by a phone status message (see extensions).

### Audio

* Uplink: Opus, 16 kHz mono, 60 ms frames, one frame per binary WebSocket message
  (protocol v1 = raw Opus; v2/v3 headers as in the xiaozhi doc are also handled).
* Downlink (TTS): Opus, **24 kHz** mono, 60 ms frames (the board's codec runs at 24 kHz → no
  resampling on the ESP32). Sent paced at real time with `voice.tts_lead_frames` (5) ahead.
  The device only plays audio while in the *speaking* state, so the server sends `tts start`,
  waits 100 ms, then streams.

### One push-to-talk turn

```
device                                         server
  │ {"type":"listen","state":"start","mode":"manual"}   (BOOT pressed)
  │ ── Opus frames ──────────────────────────▶ buffer + decode
  │ {"type":"listen","state":"stop"}           (BOOT released)
  │                                            < min_utterance_ms (400) → status idle, done
  │ ◀── {"type":"shuttle","event":"status","state":"thinking","text":"识别中…"}
  │                                            ASR(pcm 16 kHz)
  │ ◀── {"type":"stt","text":"打开设置"}
  │ ◀── {"type":"llm","emotion":"thinking","text":"🤔"}
  │ ◀── {"type":"shuttle","event":"status","state":"thinking","text":"打开设置"}
  │                                            Agent loop: screenshot → decide → act
  │ ◀── {"type":"shuttle","event":"status","state":"acting","text":"打开设置"}   (per step)
  │ ◀── {"type":"shuttle","event":"status","state":"done","text":"好了，打开设置"}
  │ ◀── {"type":"tts","state":"start"}
  │ ◀── {"type":"tts","state":"sentence_start","text":"好了，打开设置"}
  │ ◀── Opus frames (24 kHz) …
  │ ◀── {"type":"tts","state":"stop"}          device → idle
```

* Pressing BOOT again during a task: device sends `abort` (if speaking) and `listen start`; the
  server stops TTS and, with `voice.cancel_on_new_utterance`, cancels the running agent task.
* `{"type":"abort"}` alone stops TTS only.
* Errors: `status error` with a short reason (`没听清`, `手机未连接`, `执行超时`, `出错：…`) and a
  spoken sentence.

### Shuttle extension messages (server → device)

Unknown to stock xiaozhi; the Shuttle firmware handles them through the
`ShuttleHandleIncomingJson()` hook. All carry `"type":"shuttle"` and an `event`:

| event | fields | device behaviour |
|---|---|---|
| `status` | `state`: `thinking` \| `acting` \| `done` \| `error` \| `idle`; `text` (optional) | state bar 思考中 / 执行中 / 完成 / 错误 / 待命; `text` in the message area. thinking/acting stay up to 5 min, done/error 60 s, even if the device returns to idle |
| `phone` | `adb`: `device` \| `unauthorized` \| `offline` \| `absent`; `link`: `none` \| `connecting` \| `connected` \| `unreachable` | idle subtitle + hint, e.g. "请手机授权" |
| `ping` | – | nothing (resets xiaozhi's 120 s receive timeout) |

E-paper state words: 连接中 (xiaozhi connecting), 录音中 (listening), 思考中, 执行中, 完成, 错误,
播报中 (speaking without a task result), 待命 (idle).

---

## 3. `/xiaozhi/ota/` — boot-time check

xiaozhi firmware POSTs its system info (JSON, `application.version` etc.) at boot. The Shuttle
server replies (no secrets — the endpoint is unauthenticated):

```json
{"server_time":{"timestamp":1792120000000,"timezone_offset":480},
 "firmware":{"version":"<same as device>","url":""},
 "websocket":{}}
```

* `websocket: {}` keeps the URL/token the firmware provisioned from Kconfig
  (`CONFIG_SHUTTLE_SERVER_URL/TOKEN` → NVS `websocket.url/token`).
* Remote firmware update: put `<x.y.z>.bin` (the app image, `build/xiaozhi.bin`) into
  `ota.firmware_dir`; when x.y.z is newer than the device's version the reply contains
  `firmware.url = https://<host>/firmware/<x.y.z>.bin` and xiaozhi upgrades itself.
* The device works without this endpoint (`CONFIG_SHUTTLE_SKIP_OTA_CHECK=y`), at the cost of
  no clock sync.

---

## 4. `/api/phone/*` — phone API (for external agents)

Token-protected. `?device=<Device-Id>` selects the phone (default: most recently active tunnel,
else `adb.fallback_serial`). Coordinates are **screenshot pixels** (the server rescales).

| Method & path | Body (JSON) | Returns |
|---|---|---|
| `GET /api/phone/screenshot` | – | `image/jpeg` ≤ 200 KB; headers `X-Width/X-Height` (JPEG) and `X-Device-Width/X-Device-Height` |
| `POST /api/phone/tap` | `{"x":360,"y":800}` | `{"ok":true}` |
| `POST /api/phone/long_press` | `{"x":..,"y":..,"duration_ms":800}` | |
| `POST /api/phone/swipe` | `{"x1":..,"y1":..,"x2":..,"y2":..,"duration_ms":300}` | |
| `POST /api/phone/scroll` | `{"direction":"down"}` (down/up/left/right = content direction) | |
| `POST /api/phone/text` | `{"text":"你好"}` | `{"ok":true,"method":"clipboard"}` |
| `POST /api/phone/back` / `home` / `enter` | – | |
| `POST /api/phone/key` | `{"code":187}` | |
| `POST /api/phone/launch` | `{"package":"com.android.settings"}` | |
| `GET /api/phone/ui` | – | `{"elements":[{"text","desc","id","class","clickable","bounds","center"}]}` (screenshot space) |
| `GET /api/phone/ui_xml` | – | raw `uiautomator dump` XML (device space) |
| `GET /api/phone/info` | – | `{"serial","ready","current_app"}` |

Errors: 503 `{"ok":false,"error":"手机未连接…"}` when no phone, 400 for bad input / adb errors.

```bash
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/api/phone/screenshot -o s.jpg
curl -H "Authorization: Bearer $TOKEN" -d '{"x":360,"y":800}' http://127.0.0.1:8765/api/phone/tap
```

## 5. `/health`

```json
{"status":"ok","version":"0.1.0","uptime_s":3600,"adb":true,
 "devices_ctrl":1,"devices_adb":1,"phones_ready":1}
```
