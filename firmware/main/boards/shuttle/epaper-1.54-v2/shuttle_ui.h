// Shuttle e-paper UI: pure LVGL (no ESP-IDF / xiaozhi dependencies), so the exact same code
// runs in the firmware and in the desktop renderer (tools/ui_sim) that produces previews.
//
// 200x200 layout - the octopus mascot *is* the state indicator (no state words):
//   y   2-16   status row, top-right: wifi·home·phone·cloud-off  85% [battery]
//   y   6-66   mascot 78x60 (top-left)  ◁ speech bubble (2 lines): the idle hint, or
//                                         what the user said while a task runs
//   y  72      dotted divider
//   y  77-198  message: step / reply / problem, wraps, "..." when too long
//
// E-paper rules: nothing scrolls; labels are only touched when their text changes; mascot
// animations are slow (>= 0.6 s/frame), stop after a while, and idle turns into sleep.
// All methods must be called with the LVGL lock held.
#pragma once

#include <lvgl.h>

#include <cstdint>
#include <string>

class ShuttleUi {
public:
    enum class Mood { kIdle, kSleep, kConnecting, kListening, kThinking, kActing, kSpeaking, kDone, kError, kOffline };

    struct Fonts {
        const lv_font_t* text;   // CJK, ~20 px (xiaozhi theme text font)
        const lv_font_t* small;  // ASCII, 14 px 1-bpp (battery percentage)
        const lv_font_t* icon;   // Material Symbols, 14 px 1-bpp
    };

    struct Timing {
        uint32_t idle_blink_ms = 90000;     // blink once in a while when idle
        uint32_t sleep_after_ms = 600000;   // idle -> sleep (static) after 10 min
        uint32_t max_animation_ms = 120000; // stop animating a mood after 2 min
    };

    void Create(lv_obj_t* parent, int width, int height, const Fonts& fonts);
    void SetFonts(const Fonts& fonts);
    void SetTiming(const Timing& t) { timing_ = t; }

    void SetMood(Mood mood);
    void SetContext(const std::string& text);  // speech bubble text; "" hides the bubble
    void SetMessage(const std::string& text);
    void SetBattery(int percent, bool charging);  // percent < 0: hide
    void SetWifiIcon(const char* icon);            // Material Symbols glyph, nullptr/"" = none
    void SetPhoneConnected(bool ok);
    void SetHomeNetwork(bool home);  // house icon when the phone is reached via the router
    void SetServerConnected(bool ok);

    // Advance animations; called by an internal lv_timer (or manually by the simulator).
    void Tick(uint32_t now_ms);

    Mood mood() const { return mood_; }
    bool animating() const { return animating_; }
    const std::string& message() const { return message_; }
    const std::string& context() const { return context_; }

    // Exposed so xiaozhi's LvglDisplay::UpdateStatusBar can drive the Wi-Fi icon directly.
    lv_obj_t* wifi_label() const { return wifi_label_; }

private:
    static void TimerCb(lv_timer_t* timer);
    void ShowFrame(int index);
    void SetText(lv_obj_t* label, const std::string& text);
    void SetIcon(lv_obj_t* label, const char* icon);
    void ApplyFonts();
    void Relayout();

    Fonts fonts_{};
    Timing timing_{};
    int width_ = 200;
    int height_ = 200;

    lv_obj_t* icons_ = nullptr;  // flex row: wifi, home, phone, cloud-off
    lv_obj_t* wifi_label_ = nullptr;
    lv_obj_t* home_label_ = nullptr;
    lv_obj_t* phone_label_ = nullptr;
    lv_obj_t* cloud_label_ = nullptr;
    lv_obj_t* battery_pct_ = nullptr;
    lv_obj_t* battery_body_ = nullptr;
    lv_obj_t* battery_fill_ = nullptr;
    lv_obj_t* battery_nub_ = nullptr;
    lv_obj_t* mascot_ = nullptr;
    lv_obj_t* divider_ = nullptr;
    lv_obj_t* bubble_ = nullptr;
    lv_obj_t* bubble_tail_ = nullptr;
    lv_obj_t* context_label_ = nullptr;
    lv_obj_t* message_label_ = nullptr;
    lv_timer_t* timer_ = nullptr;

    Mood mood_ = Mood::kIdle;
    int frame_ = -1;
    uint32_t mood_since_ms_ = 0;
    uint32_t last_frame_ms_ = 0;
    uint32_t now_ms_ = 0;
    bool animating_ = false;
    bool bubble_shown_ = true;
    int battery_percent_ = -2;
    std::string message_;
    std::string context_;
};
