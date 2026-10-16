#include "shuttle_display.h"

#include <esp_log.h>
#include <esp_lvgl_port.h>
#include <material_symbols.h>

#include <cstring>

#include "assets/lang_config.h"
#include "board.h"
#include "lvgl_theme.h"

#define TAG "ShuttleDisplay"

namespace {

constexpr int kTopBarH = 26;
constexpr int kStateY = 30;
constexpr int kStateH = 32;
constexpr int kMessageY = 66;
constexpr int kMessageH = 104;
constexpr int kBottomY = 174;

// Ghost-clearing policy for the partial-refresh waveform.
constexpr int kFullRefreshEveryPartials = 40;
constexpr int kFullRefreshAfterTaskPartials = 12;
constexpr int64_t kStickyDoneUs = 60LL * 1000 * 1000;
constexpr int64_t kStickyBusyUs = 5LL * 60 * 1000 * 1000;

bool IsClock(const char* s) {
    // "HH:MM" as produced by LvglDisplay::UpdateStatusBar in idle state.
    return s != nullptr && strlen(s) == 5 && s[2] == ':' && s[0] >= '0' && s[0] <= '9' && s[1] >= '0' &&
           s[1] <= '9' && s[3] >= '0' && s[3] <= '9' && s[4] >= '0' && s[4] <= '9';
}

lv_obj_t* CreatePlainBox(lv_obj_t* parent, int x, int y, int w, int h) {
    lv_obj_t* obj = lv_obj_create(parent);
    lv_obj_set_pos(obj, x, y);
    lv_obj_set_size(obj, w, h);
    lv_obj_set_style_radius(obj, 0, 0);
    lv_obj_set_style_pad_all(obj, 0, 0);
    lv_obj_set_style_border_width(obj, 0, 0);
    lv_obj_set_style_bg_color(obj, lv_color_white(), 0);
    lv_obj_set_style_bg_opa(obj, LV_OPA_COVER, 0);
    lv_obj_set_scrollbar_mode(obj, LV_SCROLLBAR_MODE_OFF);
    lv_obj_remove_flag(obj, LV_OBJ_FLAG_SCROLLABLE);
    return obj;
}

}  // namespace

ShuttleDisplay::ShuttleDisplay(int width, int height, custom_lcd_spi_t spi)
    : CustomLcdDisplay(nullptr, nullptr, width, height, 0, 0, false, false, false, spi) {
    esp_timer_create_args_t args = {
        .callback = OnNotificationTimer,
        .arg = this,
        .dispatch_method = ESP_TIMER_TASK,
        .name = "shuttle_notify",
        .skip_unhandled_events = true,
    };
    esp_timer_create(&args, &shuttle_notification_timer_);
    last_full_refresh_us_ = esp_timer_get_time();
}

ShuttleDisplay::~ShuttleDisplay() {
    if (shuttle_notification_timer_ != nullptr) {
        esp_timer_stop(shuttle_notification_timer_);
        esp_timer_delete(shuttle_notification_timer_);
    }
}

void ShuttleDisplay::SetupUI() {
    if (setup_ui_called_) {
        return;
    }
    Display::SetupUI();  // marks setup_ui_called_
    DisplayLockGuard lock(this);

    auto theme = static_cast<LvglTheme*>(current_theme_);
    const lv_font_t* text_font = theme->text_font()->font();
    const lv_font_t* icon_font = theme->icon_font()->font();

    lv_obj_t* screen = lv_screen_active();
    lv_obj_set_style_bg_color(screen, lv_color_white(), 0);
    lv_obj_set_style_text_color(screen, lv_color_black(), 0);
    lv_obj_set_style_text_font(screen, text_font, 0);
    lv_obj_remove_flag(screen, LV_OBJ_FLAG_SCROLLABLE);

    // ---- top bar ----
    top_bar_ = CreatePlainBox(screen, 0, 0, width_, kTopBarH);
    network_label_ = lv_label_create(top_bar_);
    lv_obj_set_style_text_font(network_label_, icon_font, 0);
    lv_label_set_text(network_label_, "");
    lv_obj_align(network_label_, LV_ALIGN_LEFT_MID, 2, 0);

    phone_icon_ = lv_label_create(top_bar_);
    lv_obj_set_style_text_font(phone_icon_, icon_font, 0);
    lv_label_set_text(phone_icon_, "");
    lv_obj_align(phone_icon_, LV_ALIGN_LEFT_MID, 26, 0);

    mute_label_ = lv_label_create(top_bar_);  // required by LvglDisplay::UpdateStatusBar
    lv_obj_set_style_text_font(mute_label_, icon_font, 0);
    lv_label_set_text(mute_label_, "");
    lv_obj_align(mute_label_, LV_ALIGN_LEFT_MID, 50, 0);

    clock_label_ = lv_label_create(top_bar_);
    lv_label_set_text(clock_label_, "");
    lv_obj_align(clock_label_, LV_ALIGN_CENTER, 0, 0);

    battery_label_ = lv_label_create(top_bar_);
    lv_obj_set_style_text_font(battery_label_, icon_font, 0);
    lv_label_set_text(battery_label_, "");
    lv_obj_align(battery_label_, LV_ALIGN_RIGHT_MID, -2, 0);

    battery_pct_label_ = lv_label_create(top_bar_);
    lv_label_set_text(battery_pct_label_, "");
    lv_obj_align(battery_pct_label_, LV_ALIGN_RIGHT_MID, -26, 0);

    lv_obj_t* line1 = CreatePlainBox(screen, 0, kTopBarH + 1, width_, 1);
    lv_obj_set_style_bg_color(line1, lv_color_black(), 0);

    // ---- state bar (inverted) ----
    state_bar_ = CreatePlainBox(screen, 4, kStateY, width_ - 8, kStateH);
    lv_obj_set_style_bg_color(state_bar_, lv_color_black(), 0);
    lv_obj_set_style_radius(state_bar_, 6, 0);
    state_label_ = lv_label_create(state_bar_);
    lv_obj_set_style_text_color(state_label_, lv_color_white(), 0);
    lv_obj_set_width(state_label_, width_ - 16);
    lv_obj_set_style_text_align(state_label_, LV_TEXT_ALIGN_CENTER, 0);
    lv_label_set_long_mode(state_label_, LV_LABEL_LONG_MODE_DOTS);
    lv_label_set_text(state_label_, "启动中");
    lv_obj_center(state_label_);

    // ---- message area ----
    message_label_ = lv_label_create(screen);
    lv_obj_set_pos(message_label_, 4, kMessageY);
    lv_obj_set_size(message_label_, width_ - 8, kMessageH);
    lv_label_set_long_mode(message_label_, LV_LABEL_LONG_MODE_DOTS);  // wrap + "..." when too long
    lv_label_set_text(message_label_, "");

    // Hidden labels the upstream LvglDisplay code expects to exist.
    status_label_ = lv_label_create(screen);
    lv_obj_add_flag(status_label_, LV_OBJ_FLAG_HIDDEN);
    notification_label_ = lv_label_create(screen);
    lv_obj_add_flag(notification_label_, LV_OBJ_FLAG_HIDDEN);

    // ---- bottom bar ----
    lv_obj_t* line2 = CreatePlainBox(screen, 0, kBottomY - 2, width_, 1);
    lv_obj_set_style_bg_color(line2, lv_color_black(), 0);
    bottom_bar_ = CreatePlainBox(screen, 0, kBottomY, width_, height_ - kBottomY);
    link_label_ = lv_label_create(bottom_bar_);
    lv_obj_set_width(link_label_, width_ - 30);
    lv_label_set_long_mode(link_label_, LV_LABEL_LONG_MODE_DOTS);
    lv_label_set_text(link_label_, "手机未连接");
    lv_obj_align(link_label_, LV_ALIGN_LEFT_MID, 4, 0);
    server_icon_ = lv_label_create(bottom_bar_);
    lv_obj_set_style_text_font(server_icon_, icon_font, 0);
    lv_label_set_text(server_icon_, MATERIAL_SYMBOLS_CLOUD_OFF);
    lv_obj_align(server_icon_, LV_ALIGN_RIGHT_MID, -2, 0);

    lv_display_add_event_cb(display_, OnFlushStart, LV_EVENT_FLUSH_START, this);
    refresh_timer_ = lv_timer_create(OnRefreshTimer, 500, this);
}

void ShuttleDisplay::ApplyFonts() {
    auto theme = static_cast<LvglTheme*>(current_theme_);
    if (theme == nullptr || state_label_ == nullptr) {
        return;
    }
    const lv_font_t* text_font = theme->text_font()->font();
    const lv_font_t* icon_font = theme->icon_font()->font();
    lv_obj_set_style_text_font(lv_screen_active(), text_font, 0);
    for (lv_obj_t* l : {network_label_, phone_icon_, mute_label_, battery_label_, server_icon_}) {
        lv_obj_set_style_text_font(l, icon_font, 0);
    }
}

void ShuttleDisplay::SetTheme(Theme* theme) {
    // Black/white panel: always render the light palette, but adopt the theme's fonts
    // (Assets::Apply() swaps in the larger "common" CJK font from the assets partition).
    DisplayLockGuard lock(this);
    current_theme_ = theme;
    ApplyFonts();
}

void ShuttleDisplay::SetLabelText(lv_obj_t* label, const std::string& text) {
    if (label == nullptr) {
        return;
    }
    const char* current = lv_label_get_text(label);
    if (current != nullptr && text == current) {
        return;  // avoid a needless e-paper refresh
    }
    lv_label_set_text(label, text.c_str());
}

void ShuttleDisplay::SetStateText(const std::string& text) {
    ESP_LOGI(TAG, "State: %s", text.c_str());
    SetLabelText(state_label_, text);
}

void ShuttleDisplay::SetMessageText(const std::string& text) {
    message_ = text;
    if (notification_.empty()) {
        SetLabelText(message_label_, message_);
    }
}

bool ShuttleDisplay::StickyActive() const { return esp_timer_get_time() < sticky_until_us_; }

void ShuttleDisplay::SetStatus(const char* status) {
    if (status == nullptr) {
        return;
    }
    DisplayLockGuard lock(this);
    if (IsClock(status)) {
        SetLabelText(clock_label_, status);
        return;
    }
    if (strcmp(status, Lang::Strings::CONNECTING) == 0) {
        sticky_until_us_ = 0;
        SetStateText("连接中");
    } else if (strcmp(status, Lang::Strings::LISTENING) == 0) {
        sticky_until_us_ = 0;
        task_state_.clear();
        SetStateText("录音中");
        SetMessageText("");
    } else if (strcmp(status, Lang::Strings::SPEAKING) == 0) {
        SetStateText(task_state_ == "done" ? "完成" : (task_state_ == "error" ? "错误" : "播报中"));
    } else if (strcmp(status, Lang::Strings::STANDBY) == 0) {
        if (!StickyActive()) {
            task_state_.clear();
            SetStateText("待命");
        }
    } else if (strcmp(status, Lang::Strings::ERROR) == 0) {
        SetStateText("错误");
    } else {
        SetStateText(status);
    }
}

void ShuttleDisplay::ShowNotification(const std::string& notification, int duration_ms) {
    ShowNotification(notification.c_str(), duration_ms);
}

void ShuttleDisplay::ShowNotification(const char* notification, int duration_ms) {
    if (notification == nullptr) {
        return;
    }
    DisplayLockGuard lock(this);
    ESP_LOGI(TAG, "Notification: %s", notification);
    if (StickyActive()) {
        return;  // do not hide the task result behind e.g. "已连接 xxx"
    }
    notification_ = notification;
    SetLabelText(message_label_, notification_);
    esp_timer_stop(shuttle_notification_timer_);
    esp_timer_start_once(shuttle_notification_timer_, static_cast<uint64_t>(duration_ms) * 1000);
}

void ShuttleDisplay::OnNotificationTimer(void* arg) {
    auto* self = static_cast<ShuttleDisplay*>(arg);
    DisplayLockGuard lock(self);
    self->notification_.clear();
    self->SetLabelText(self->message_label_, self->message_);
}

void ShuttleDisplay::SetEmotion(const char* emotion) {
    // No emoji area on this layout; the state bar carries the meaning.
}

void ShuttleDisplay::SetChatMessage(const char* role, const char* content) {
    DisplayLockGuard lock(this);
    if (content == nullptr || content[0] == '\0') {
        if (!StickyActive()) {
            SetMessageText("");
        }
        return;
    }
    if (role != nullptr && strcmp(role, "user") == 0) {
        SetMessageText(std::string("「") + content + "」");
    } else {
        SetMessageText(content);
    }
}

void ShuttleDisplay::ClearChatMessages() {
    DisplayLockGuard lock(this);
    if (!StickyActive()) {
        SetMessageText("");
    }
}

void ShuttleDisplay::UpdateStatusBar(bool update_all) {
    // Upstream logic: network icon, battery icon, mute icon and idle clock (via SetStatus).
    LvglDisplay::UpdateStatusBar(update_all);

    // Battery percentage, quantised to 5% so ADC noise does not refresh the panel.
    int level = 0;
    bool charging = false, discharging = false;
    if (Board::GetInstance().GetBatteryLevel(level, charging, discharging)) {
        int q = (level + 2) / 5 * 5;
        if (update_all || q != last_battery_pct_) {
            last_battery_pct_ = q;
            DisplayLockGuard lock(this);
            SetLabelText(battery_pct_label_, std::to_string(q) + "%");
        }
    }

    // Periodic ghost cleanup while idle (at most every 30 min, only if something changed).
    int64_t now = esp_timer_get_time();
    if (partial_refreshes_ > 0 && now - last_full_refresh_us_ > 30LL * 60 * 1000 * 1000 && !StickyActive()) {
        RequestFullRefresh();
    }
}

void ShuttleDisplay::SetTaskState(const std::string& state, const std::string& text) {
    DisplayLockGuard lock(this);
    task_state_ = state;
    int64_t now = esp_timer_get_time();
    if (state == "thinking") {
        sticky_until_us_ = now + kStickyBusyUs;
        SetStateText("思考中");
    } else if (state == "acting") {
        sticky_until_us_ = now + kStickyBusyUs;
        SetStateText("执行中");
    } else if (state == "done") {
        sticky_until_us_ = now + kStickyDoneUs;
        SetStateText("完成");
    } else if (state == "error") {
        sticky_until_us_ = now + kStickyDoneUs;
        SetStateText("错误");
    } else {  // idle / unknown
        sticky_until_us_ = 0;
        task_state_.clear();
        SetStateText("待命");
    }
    if (!text.empty()) {
        notification_.clear();
        esp_timer_stop(shuttle_notification_timer_);
        SetMessageText(text);
    }
    if ((state == "done" || state == "error") && partial_refreshes_ >= kFullRefreshAfterTaskPartials) {
        full_refresh_pending_ = true;
    }
}

void ShuttleDisplay::SetPhoneLink(bool ok, const std::string& detail) {
    DisplayLockGuard lock(this);
    SetLabelText(phone_icon_, ok ? MATERIAL_SYMBOLS_PHONE : "");
    SetLabelText(link_label_, detail);
}

void ShuttleDisplay::SetServerLink(bool ok) {
    DisplayLockGuard lock(this);
    SetLabelText(server_icon_, ok ? MATERIAL_SYMBOLS_LINK : MATERIAL_SYMBOLS_CLOUD_OFF);
}

void ShuttleDisplay::RequestFullRefresh() { full_refresh_pending_ = true; }

void ShuttleDisplay::OnFlushStart(lv_event_t* e) {
    auto* self = static_cast<ShuttleDisplay*>(lv_event_get_user_data(e));
    self->partial_refreshes_++;
    if (self->partial_refreshes_ >= kFullRefreshEveryPartials) {
        self->full_refresh_pending_ = true;
    }
}

void ShuttleDisplay::OnRefreshTimer(lv_timer_t* timer) {
    // Runs inside the LVGL task with the port lock held, so no flush can interleave.
    auto* self = static_cast<ShuttleDisplay*>(lv_timer_get_user_data(timer));
    if (!self->full_refresh_pending_) {
        return;
    }
    self->full_refresh_pending_ = false;
    ESP_LOGI(TAG, "Full refresh (after %d partial refreshes)", self->partial_refreshes_);
    // The driver's frame buffer still holds the last rendered frame; push it with the
    // full waveform (clears ghosting), then re-arm the partial-refresh waveform.
    self->EPD_Init();
    self->EPD_DisplayPartBaseImage();
    self->EPD_Init_Partial();
    self->partial_refreshes_ = 0;
    self->last_full_refresh_us_ = esp_timer_get_time();
}
