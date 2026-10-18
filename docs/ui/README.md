# E-paper UI

200×200 black/white e-paper. A pixel-art octopus in the top-left corner shows what the device is
doing — its mood *is* the state, there are no state words. Next to it a speech bubble holds the
hint (idle) or what you said (while a task runs); the result/steps go below the dotted line.
Top-right: Wi-Fi · 🏠 (phone via router) · phone · ☁̸ (server down) · battery.

![screens](screens.png)

| Listening | Thinking | Acting | Speaking |
|---|---|---|---|
| ![](listening.gif) | ![](thinking.gif) | ![](acting.gif) | ![](speaking.gif) |

All moods/frames: ![mascot](mascot_sheet.png)

These images are **rendered by the real firmware UI code** (`shuttle_ui.cc`) on LVGL 9.5 with
the device fonts, converted to black/white with the same rule as the e-paper driver — so they
are what the panel shows (modulo e-paper contrast/ghosting).

## Where things live

| File | Role |
|---|---|
| `firmware/main/boards/shuttle/epaper-1.54-v2/shuttle_ui.{h,cc}` | UI layout + mascot animation (pure LVGL, runs on device and desktop) |
| `firmware/main/boards/shuttle/epaper-1.54-v2/shuttle_display.{h,cc}` | maps xiaozhi states / server `shuttle` messages / ADB link to the UI; e-paper refresh policy |
| `firmware/main/boards/shuttle/epaper-1.54-v2/mascot_frames.h` | generated mascot bitmaps (LVGL 1-bit images) |
| `tools/mascot/gen_mascot.py` | the mascot pixel art (edit here, re-run) |
| `tools/ui_sim/run.sh` | desktop renderer → `docs/ui/*.png|gif` (also runs in CI: artifact `ui-preview`) |

## States

| Mood | When | Bubble / message |
|---|---|---|
| idle (blinks every 90 s; sleep with "z" after 10 min) | ready | 按住灯泡键说话 / last result stays below |
| offline (droopy eyes) | phone or server link down | problem + what to do in the message |
| connecting (eyes look around) | voice channel opening, boot, Wi-Fi portal | …… / boot step |
| listening (sound waves) | light-bulb key held | 松开发送 |
| thinking (thought bubbles) | ASR / agent planning | …… → what you said |
| acting (tentacles + phone) | agent working the phone (`status acting`) | 第 N 步：… |
| speaking (mouth) / done (^ ^ + sparkles) | TTS playing / result (`status done`) | the reply |
| error (X eyes, sweat drop) | errors | what went wrong |

E-paper friendly by design: 1-bit art (no grey dithering), slow frames (0.6–1 s), animations
stop after 2 min, labels only redraw when their text changes, the ghost-clearing full refresh
never runs mid-animation.

## Changing it

```bash
$EDITOR tools/mascot/gen_mascot.py        # pixel art: 26×20 grid, scaled ×3
$EDITOR firmware/main/boards/shuttle/epaper-1.54-v2/shuttle_ui.cc   # layout
tools/ui_sim/run.sh                       # re-render previews (needs a C/C++ compiler, Pillow)
```
Then build/flash the firmware as usual. Keep text short: the bubble fits 2 lines of ~5 Chinese
characters, the message area 4 lines of ~9. On the device, `CONFIG_SHUTTLE_DEBUG_SCREEN_DUMP=y`
+ `tools/screen_dump.py` give the real panel content.
