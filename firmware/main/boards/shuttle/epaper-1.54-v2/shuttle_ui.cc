#include "shuttle_ui.h"

#include <material_symbols.h>

#include "mascot_frames.h"

namespace {

struct MoodAnim {
    const lv_image_dsc_t* const* frames;
    int count;
    uint32_t interval_ms;  // 0 = static
};

#define ANIM(name, ms) {mascot_##name##_frames, (int)(sizeof(mascot_##name##_frames) / sizeof(mascot_##name##_frames[0])), ms}

// Order matches ShuttleUi::Mood.
const MoodAnim kAnims[] = {
    ANIM(idle, 0),          // blink handled separately
    ANIM(sleep, 0),
    ANIM(connecting, 1000),
    ANIM(listening, 700),
    ANIM(thinking, 900),
    ANIM(acting, 900),
    ANIM(speaking, 600),
    ANIM(done, 0),
    ANIM(error, 0),
    ANIM(offline, 0),
};

constexpr uint32_t kBlinkMs = 450;
constexpr uint32_t kTickMs = 200;

constexpr int kBarY = 2;
constexpr int kMascotX = 4;
constexpr int kMascotY = 24;
constexpr int kColumnX = MASCOT_W + 12;
constexpr int kDividerY = 92;
constexpr int kMessageY = 98;

lv_obj_t* MakeLabel(lv_obj_t* parent, const lv_font_t* font) {
    lv_obj_t* l = lv_label_create(parent);
    lv_obj_set_style_text_color(l, lv_color_black(), 0);
    if (font != nullptr) {
        lv_obj_set_style_text_font(l, font, 0);
    }
    lv_label_set_text(l, "");
    return l;
}

int LineHeight(const lv_font_t* font) { return font != nullptr ? lv_font_get_line_height(font) : 24; }

lv_obj_t* MakeBox(lv_obj_t* parent, int x, int y, int w, int h) {
    lv_obj_t* o = lv_obj_create(parent);
    lv_obj_remove_style_all(o);
    lv_obj_set_pos(o, x, y);
    lv_obj_set_size(o, w, h);
    lv_obj_remove_flag(o, LV_OBJ_FLAG_SCROLLABLE);
    return o;
}

}  // namespace

const char* ShuttleUi::DefaultTitle(Mood mood) {
    switch (mood) {
        case Mood::kIdle: return "待命";
        case Mood::kSleep: return "休息中";
        case Mood::kConnecting: return "连接中";
        case Mood::kListening: return "录音中";
        case Mood::kThinking: return "思考中";
        case Mood::kActing: return "执行中";
        case Mood::kSpeaking: return "播报中";
        case Mood::kDone: return "完成";
        case Mood::kError: return "出错了";
        case Mood::kOffline: return "待命";
    }
    return "";
}

void ShuttleUi::Create(lv_obj_t* parent, int width, int height, const Fonts& fonts) {
    fonts_ = fonts;
    width_ = width;
    height_ = height;

    lv_obj_set_style_bg_color(parent, lv_color_white(), 0);
    lv_obj_set_style_bg_opa(parent, LV_OPA_COVER, 0);
    lv_obj_set_style_text_color(parent, lv_color_black(), 0);
    lv_obj_remove_flag(parent, LV_OBJ_FLAG_SCROLLABLE);
    if (fonts.text != nullptr) {
        lv_obj_set_style_text_font(parent, fonts.text, 0);
    }

    // ---- status bar ----
    wifi_label_ = MakeLabel(parent, fonts.icon);
    lv_obj_set_pos(wifi_label_, 4, kBarY);
    phone_label_ = MakeLabel(parent, fonts.icon);
    lv_obj_set_pos(phone_label_, 22, kBarY);
    cloud_label_ = MakeLabel(parent, fonts.icon);
    lv_obj_set_pos(cloud_label_, 40, kBarY);

    clock_label_ = MakeLabel(parent, fonts.small);
    lv_obj_align(clock_label_, LV_ALIGN_TOP_MID, 0, kBarY);

    // battery: [=====]▪ drawn with boxes, crisp at any level
    battery_body_ = MakeBox(parent, width - 25, kBarY + 3, 19, 10);
    lv_obj_set_style_border_width(battery_body_, 1, 0);
    lv_obj_set_style_border_color(battery_body_, lv_color_black(), 0);
    lv_obj_set_style_radius(battery_body_, 2, 0);
    battery_fill_ = MakeBox(battery_body_, 2, 2, 15, 6);
    lv_obj_set_style_bg_color(battery_fill_, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(battery_fill_, LV_OPA_COVER, 0);
    battery_nub_ = MakeBox(parent, width - 6, kBarY + 6, 2, 4);
    lv_obj_set_style_bg_color(battery_nub_, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(battery_nub_, LV_OPA_COVER, 0);
    battery_pct_ = MakeLabel(parent, fonts.small);
    lv_obj_align(battery_pct_, LV_ALIGN_TOP_RIGHT, -29, kBarY);

    // ---- mascot + title column ----
    mascot_ = lv_image_create(parent);
    lv_obj_set_pos(mascot_, kMascotX, kMascotY);

    // Title: small inverted "pill" (white on black) = the state, readable at a glance.
    title_ = MakeLabel(parent, fonts.text);
    lv_obj_set_pos(title_, kColumnX, kMascotY + 4);
    lv_obj_set_style_text_color(title_, lv_color_white(), 0);
    lv_obj_set_style_bg_color(title_, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(title_, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(title_, 6, 0);
    lv_obj_set_style_pad_hor(title_, 7, 0);
    lv_obj_set_style_pad_ver(title_, 1, 0);
    lv_obj_set_style_max_width(title_, width - kColumnX - 4, 0);
    lv_label_set_long_mode(title_, LV_LABEL_LONG_MODE_CLIP);

    // Subtitle: one line, "..." when too long.
    subtitle_ = MakeLabel(parent, fonts.text);
    lv_obj_set_pos(subtitle_, kColumnX + 1, kMascotY + 36);
    lv_obj_set_size(subtitle_, width - kColumnX - 3, LineHeight(fonts.text));
    lv_label_set_long_mode(subtitle_, LV_LABEL_LONG_MODE_DOTS);

    // ---- dotted divider ----
    static lv_point_precise_t points[] = {{0, 0}, {0, 0}};
    points[1].x = width - 16;
    divider_ = lv_line_create(parent);
    lv_line_set_points(divider_, points, 2);
    lv_obj_set_pos(divider_, 8, kDividerY);
    lv_obj_set_style_line_width(divider_, 1, 0);
    lv_obj_set_style_line_color(divider_, lv_color_black(), 0);
    lv_obj_set_style_line_dash_width(divider_, 2, 0);
    lv_obj_set_style_line_dash_gap(divider_, 3, 0);

    // ---- message ----
    message_label_ = MakeLabel(parent, fonts.text);
    lv_obj_set_pos(message_label_, 8, kMessageY);
    lv_obj_set_size(message_label_, width - 16, height - kMessageY - 2);
    lv_label_set_long_mode(message_label_, LV_LABEL_LONG_MODE_DOTS);
    lv_obj_set_style_text_line_space(message_label_, -2, 0);

    SetMood(Mood::kIdle, DefaultTitle(Mood::kIdle), "");
    SetBattery(-1, false);
    timer_ = lv_timer_create(TimerCb, kTickMs, this);
}

void ShuttleUi::SetFonts(const Fonts& fonts) {
    fonts_ = fonts;
    ApplyFonts();
}

void ShuttleUi::ApplyFonts() {
    if (title_ == nullptr) {
        return;
    }
    if (fonts_.text != nullptr) {
        lv_obj_set_style_text_font(lv_obj_get_parent(title_), fonts_.text, 0);
        for (lv_obj_t* l : {title_, subtitle_, message_label_}) {
            lv_obj_set_style_text_font(l, fonts_.text, 0);
        }
        lv_obj_set_height(subtitle_, LineHeight(fonts_.text));
    }
    if (fonts_.small != nullptr) {
        lv_obj_set_style_text_font(clock_label_, fonts_.small, 0);
        lv_obj_set_style_text_font(battery_pct_, fonts_.small, 0);
    }
    if (fonts_.icon != nullptr) {
        for (lv_obj_t* l : {wifi_label_, phone_label_, cloud_label_}) {
            lv_obj_set_style_text_font(l, fonts_.icon, 0);
        }
    }
}

void ShuttleUi::SetText(lv_obj_t* label, const std::string& text) {
    if (label == nullptr) {
        return;
    }
    const char* cur = lv_label_get_text(label);
    if (cur != nullptr && text == cur) {
        return;  // every change costs an e-paper refresh
    }
    lv_label_set_text(label, text.c_str());
}

void ShuttleUi::ShowFrame(int index) {
    const MoodAnim& a = kAnims[static_cast<int>(mood_)];
    if (index >= a.count) {
        index = 0;
    }
    if (index != frame_) {
        lv_image_set_src(mascot_, a.frames[index]);
        frame_ = index;
    }
    last_frame_ms_ = lv_tick_get();
}

void ShuttleUi::SetMood(Mood mood, const char* title, const char* subtitle) {
    if (mood != mood_ || frame_ < 0) {
        mood_ = mood;
        frame_ = -1;
        mood_since_ms_ = lv_tick_get();
        ShowFrame(0);
        if (title == nullptr) {
            title = DefaultTitle(mood);
        }
    }
    if (title != nullptr) {
        SetTitle(title);
    }
    if (subtitle != nullptr) {
        SetSubtitle(subtitle);
    }
}

void ShuttleUi::SetTitle(const std::string& text) { SetText(title_, text); }

void ShuttleUi::SetSubtitle(const std::string& text) { SetText(subtitle_, text); }

void ShuttleUi::SetMessage(const std::string& text) {
    message_ = text;
    SetText(message_label_, text);
}

void ShuttleUi::SetClock(const std::string& text) { SetText(clock_label_, text); }

void ShuttleUi::SetBattery(int percent, bool charging) {
    if (percent == battery_percent_) {
        return;
    }
    battery_percent_ = percent;
    bool show = percent >= 0;
    for (lv_obj_t* o : {battery_body_, battery_nub_, battery_pct_}) {
        if (show) {
            lv_obj_remove_flag(o, LV_OBJ_FLAG_HIDDEN);
        } else {
            lv_obj_add_flag(o, LV_OBJ_FLAG_HIDDEN);
        }
    }
    if (!show) {
        return;
    }
    int p = percent > 100 ? 100 : percent;
    int w = (15 * p + 50) / 100;
    lv_obj_set_width(battery_fill_, w < 1 ? 1 : w);
    SetText(battery_pct_, std::to_string(p) + (charging ? "+" : "%"));
    lv_obj_align(battery_pct_, LV_ALIGN_TOP_RIGHT, -29, kBarY);
}

void ShuttleUi::SetWifiIcon(const char* icon) { SetText(wifi_label_, icon ? icon : ""); }

void ShuttleUi::SetPhoneConnected(bool ok) {
    SetText(phone_label_, ok ? MATERIAL_SYMBOLS_PHONE : "");
}

void ShuttleUi::SetServerConnected(bool ok) {
    SetText(cloud_label_, ok ? "" : MATERIAL_SYMBOLS_CLOUD_OFF);
}

void ShuttleUi::TimerCb(lv_timer_t* timer) {
    static_cast<ShuttleUi*>(lv_timer_get_user_data(timer))->Tick(lv_tick_get());
}

void ShuttleUi::Tick(uint32_t now_ms) {
    now_ms_ = now_ms;
    uint32_t in_mood = now_ms - mood_since_ms_;
    uint32_t since_frame = now_ms - last_frame_ms_;

    if (mood_ == Mood::kIdle) {
        animating_ = false;
        if (timing_.sleep_after_ms > 0 && in_mood > timing_.sleep_after_ms) {
            SetMood(Mood::kSleep, nullptr, nullptr);
            return;
        }
        if (frame_ == 1 && since_frame >= kBlinkMs) {
            ShowFrame(0);
        } else if (frame_ == 0 && timing_.idle_blink_ms > 0 && since_frame >= timing_.idle_blink_ms) {
            ShowFrame(1);
        }
        return;
    }

    const MoodAnim& a = kAnims[static_cast<int>(mood_)];
    if (a.count < 2 || a.interval_ms == 0) {
        animating_ = false;
        return;
    }
    if (timing_.max_animation_ms > 0 && in_mood > timing_.max_animation_ms) {
        if (frame_ != 0) {
            ShowFrame(0);
        }
        animating_ = false;
        return;
    }
    animating_ = true;
    if (since_frame >= a.interval_ms) {
        ShowFrame((frame_ + 1) % a.count);
    }
}
