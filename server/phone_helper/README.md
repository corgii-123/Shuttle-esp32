# phone_helper — optional phone-side screenshot compression & clipboard

A tiny Java program compiled to a dex jar and run with `app_process` **as the adb shell
user** — nothing is installed on the phone, and the ESP32 firmware is not involved.

| command | what it does | used by |
|---|---|---|
| `screenshot SHORT QUALITY MAXBYTES` | runs `screencap` (raw RGBA), scales + JPEG-compresses on the phone, prints `SHUTTLE1 <w> <h>\n` + JPEG | `phone.device_side_screenshot: true` |
| `clip BASE64` | sets the clipboard through the hidden `IClipboard` service (same trick as scrcpy); the server then sends `KEYCODE_PASTE` | Chinese text input (`PhoneController.input_text`) |
| `version` | prints the helper version | debugging |

Why: a raw `screencap -p` PNG is 1–3 MB; through hotspot → Internet → Cloudflare this is the
slowest step of every agent turn. Compressing on the phone sends ~100–200 KB instead.
If anything fails the server falls back to Mac-side compression automatically (and retries
the helper after 10 minutes).

## Build

```bash
# needs Android SDK platform + build-tools (d8)
brew install --cask android-commandlinetools
sdkmanager "platforms;android-35" "build-tools;35.0.0"
export ANDROID_HOME=/opt/homebrew/share/android-commandlinetools
server/phone_helper/build.sh     # -> server/shuttle_server/phone_helper/shuttle-helper.jar
```

Or download the `shuttle-helper-jar` artifact of the **Server** GitHub Actions workflow and
copy it to `server/shuttle_server/phone_helper/shuttle-helper.jar`.

## Manual test

```bash
adb push server/shuttle_server/phone_helper/shuttle-helper.jar /data/local/tmp/
adb exec-out CLASSPATH=/data/local/tmp/shuttle-helper.jar app_process /system/bin \
    com.shuttle.helper.Main screenshot 720 80 204800 > shot.bin
head -c 30 shot.bin            # SHUTTLE1 1080 2400
tail -c +$(( $(head -1 shot.bin | wc -c) + 1 )) shot.bin > shot.jpg && open shot.jpg
adb shell CLASSPATH=/data/local/tmp/shuttle-helper.jar app_process /system/bin \
    com.shuttle.helper.Main clip $(printf '你好' | base64)   # -> OK, then long-press paste
```

Not verified on real hardware yet (see HANDOFF.md): raw `screencap` header size (12/16 bytes)
and the `setPrimaryClip` signatures differ between Android versions/ROMs (MIUI/ColorOS may
block shell clipboard writes; then install ADBKeyboard or keep `input_method: auto`).
