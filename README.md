# 搞快 Shuttle

Hold a button on a pocket ESP32 e-paper gadget, say what you want, and your Android phone does
it. Speech recognition, the agent and speech synthesis run on a Mac in another city; the
ESP32 only records/plays audio, shows status, and tunnels the phone's adb connection byte for
byte. The phone needs no app.

* **firmware/** — ESP-IDF v6.1 firmware based on [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32)
  for the Waveshare ESP32-S3-ePaper-1.54 **V2**: push-to-talk, Opus, xiaozhi WebSocket protocol,
  e-paper status, multi-SSID Wi-Fi, ADB TCP⇄WebSocket bridge that finds phones by itself (no IPs to
  configure; several phones, switch by voice: "换手机" / "切换到小米").
* **server/** — Python 3.11+ aiohttp server for macOS: token-protected WebSockets, ADB tunnel
  (`adb connect 127.0.0.1:15555`), `PhoneController`, ASR (Qwen) → Agent → TTS with mocks.
* **tools/** — `sim_device.py` (simulate the device), `fake_adb.py` (simulate a phone).
* **deploy/** — launchd (boot start, crash restart, watchdog), Cloudflare Tunnel, installer.
* **docs/** — [setup guide](docs/README.md) · [protocol](docs/protocol.md)
* [CLAUDE.md](CLAUDE.md) (for Claude Code) · [HANDOFF.md](HANDOFF.md) (open risks, bring-up order)

```bash
deploy/install.sh                                         # Mac server
cd firmware && python build_shuttle.py && idf.py flash monitor   # firmware
python tools/sim_device.py --token <token> --say "打开设置" --play # test without hardware
```
