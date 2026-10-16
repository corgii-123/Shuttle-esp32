#include "shuttle_display.h"

#include <esp_log.h>
#include <esp_lvgl_port.h>

#include <cstring>

#include "assets/lang_config.h"
#include "board.h"
#include "lvgl_theme.h"

#define TAG "ShuttleDisplay"

LV_FONT_DECLARE(font_noto_sans_basic_14_1);
LV_FONT_DECLARE(font_material_symbols_14_1);

namespace {

// Ghost-clearing policy for the partial-refresh waveform.
constexpr int kFullRefreshEveryPartials = 40;
constexpr int kFullRefreshAfterTaskPartials = 12;
constexpr int64_t kStickyDoneUs = 60LL * 1000 * 1000;
constexpr int64_t kStickyBusyUs = 5LL * 60 * 1000 * 1000;
constexpr const char* kIdleHint = "按住 BOOT 说话";

bool IsClock(const char* s) {
    // "HH:MM" as produced by LvglDisplay::UpdateStatusBar in idle state.
    return s != nullptr && strlen(s) == 5 && s[2] == ':' && s[0] >= '0' && s[0] <= '9' && s[1] >= '0' &&
           s[1] <= '9' && s[3] >= '0' && s[3] <= '9' && s[4] >= '0' && s[4] <= '9';
}

lv_obj_t* HiddenLabel(lv_obj_t* parent) {
    lv_obj_t* l = lv_label_create(parent);
    lv_label_set_text(l, "");
    lv_obj_add_flag(l, LV_OBJ_FLAG_HIDDEN);
    return l;
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

ShuttleUi::Fonts ShuttleDisplay::CurrentFonts() const {
    auto theme = static_cast<LvglTheme*>(current_theme_);
    // Text: theme font (the assets partition swaps in the CJK "common" font). Status bar:
    // 14 px 1-bpp fonts linked into the firmware - crisp on a black/white panel.
    return {theme->text_font()->font(), &font_noto_sans_basic_14_1, &font_material_symbols_14_1};
}

void ShuttleDisplay::SetupUI() {
    if (setup_ui_called_) {
        return;
    }
    Display::SetupUI();  // marks setup_ui_called_
    DisplayLockGuard lock(this);

    lv_obj_t* screen = lv_screen_active();
    ui_.Create(screen, width_, height_, CurrentFonts());
    ui_.SetMood(ShuttleUi::Mood::kConnecting, "启动中", "");

    // Objects the upstream LvglDisplay code writes to. The Wi-Fi icon goes straight to the UI;
    // the others are hidden (battery is drawn by ShuttleUi, notifications by this class).
    network_label_ = ui_.wifi_label();
    mute_label_ = HiddenLabel(screen);
    battery_label_ = HiddenLabel(screen);
    status_label_ = HiddenLabel(screen);
    notification_label_ = HiddenLabel(screen);

    lv_display_add_event_cb(display_, OnFlushStart, LV_EVENT_FLUSH_START, this);
    refresh_timer_ = lv_timer_create(OnRefreshTimer, 500, this);
}

void ShuttleDisplay::SetTheme(Theme* theme) {
    // Black/white panel: ignore theme colours, but adopt its (possibly new) text font.
    DisplayLockGuard lock(this);
    current_theme_ = theme;
    if (setup_ui_called_) {
        ui_.SetFonts(CurrentFonts());
    }
}

bool ShuttleDisplay::StickyActive() const { return esp_timer_get_time() < sticky_until_us_; }

void ShuttleDisplay::SetMessage(const std::string& text) {
    message_ = text;
    if (notification_.empty()) {
        ui_.SetMessage(message_);
    }
}

void ShuttleDisplay::ShowIdle() {
    if (phone_ok_) {
        ui_.SetMood(ShuttleUi::Mood::kIdle, "待命", "手机已连接");
    } else {
        ui_.SetMood(ShuttleUi::Mood::kOffline, "待命", phone_detail_.c_str());
    }
    if (message_.empty()) {
        SetMessage(kIdleHint);
    }
}

void ShuttleDisplay::SetStatus(const char* status) {
    if (status == nullptr) {
        return;
    }
    DisplayLockGuard lock(this);
    if (IsClock(status)) {
        ui_.SetClock(status);
        return;
    }
    ESP_LOGI(TAG, "Status: %s", status);
    if (strcmp(status, Lang::Strings::CONNECTING) == 0) {
        sticky_until_us_ = 0;
        ui_.SetMood(ShuttleUi::Mood::kConnecting, nullptr, "");
        SetMessage("");
    } else if (strcmp(status, Lang::Strings::LISTENING) == 0) {
        sticky_until_us_ = 0;
        task_state_.clear();
        ui_.SetMood(ShuttleUi::Mood::kListening, nullptr, "松开发送");
        SetMessage("");
    } else if (strcmp(status, Lang::Strings::SPEAKING) == 0) {
        const char* title = task_state_ == "done" ? "完成" : (task_state_ == "error" ? "出错了" : "播报中");
        ui_.SetMood(ShuttleUi::Mood::kSpeaking, title, nullptr);
    } else if (strcmp(status, Lang::Strings::STANDBY) == 0) {
        if (!StickyActive()) {
            task_state_.clear();
            SetMessage("");
            ShowIdle();
        } else if (task_state_ == "done") {
            ui_.SetMood(ShuttleUi::Mood::kDone, nullptr, nullptr);
        }
    } else if (strcmp(status, Lang::Strings::ERROR) == 0) {
        ui_.SetMood(ShuttleUi::Mood::kError, nullptr, "");
    } else {
        // Boot/activation steps ("检查新版本...", "登录服务器..."): keep the mascot, show the step.
        if (ui_.mood() == ShuttleUi::Mood::kConnecting) {
            ui_.SetSubtitle(status);
        } else {
            ui_.SetTitle(status);
        }
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
    ui_.SetMessage(notification_);
    esp_timer_stop(shuttle_notification_timer_);
    esp_timer_start_once(shuttle_notification_timer_, static_cast<uint64_t>(duration_ms) * 1000);
}

void ShuttleDisplay::OnNotificationTimer(void* arg) {
    auto* self = static_cast<ShuttleDisplay*>(arg);
    DisplayLockGuard lock(self);
    self->notification_.clear();
    self->ui_.SetMessage(self->message_.empty() && (self->ui_.mood() == ShuttleUi::Mood::kIdle ||
                                                    self->ui_.mood() == ShuttleUi::Mood::kOffline)
                             ? kIdleHint
                             : self->message_);
}

void ShuttleDisplay::SetEmotion(const char* emotion) {
    // The mascot's mood follows device/task state, not xiaozhi's emoji names.
}

void ShuttleDisplay::SetChatMessage(const char* role, const char* content) {
    DisplayLockGuard lock(this);
    if (content == nullptr || content[0] == '\0') {
        if (!StickyActive()) {
            SetMessage("");
        }
        return;
    }
    if (role != nullptr && strcmp(role, "user") == 0) {
        SetMessage(std::string("「") + content + "」");
    } else {
        SetMessage(content);
    }
}

void ShuttleDisplay::ClearChatMessages() {
    DisplayLockGuard lock(this);
    if (!StickyActive()) {
        SetMessage("");
    }
}

void ShuttleDisplay::UpdateStatusBar(bool update_all) {
    // Upstream logic: Wi-Fi icon (-> ui_.wifi_label()), idle clock (via SetStatus), low battery.
    LvglDisplay::UpdateStatusBar(update_all);

    // Battery, quantised to 5% so ADC noise does not refresh the panel.
    int level = 0;
    bool charging = false, discharging = false;
    if (Board::GetInstance().GetBatteryLevel(level, charging, discharging)) {
        int q = (level + 2) / 5 * 5;
        if (update_all || q != last_battery_pct_) {
            last_battery_pct_ = q;
            DisplayLockGuard lock(this);
            ui_.SetBattery(q, charging);
        }
    }

    // Periodic ghost cleanup (at most every 30 min, only if something was drawn).
    int64_t now = esp_timer_get_time();
    if (partial_refreshes_ > 0 && now - last_full_refresh_us_ > 30LL * 60 * 1000 * 1000 && !StickyActive()) {
        RequestFullRefresh();
    }
}

void ShuttleDisplay::SetTaskState(const std::string& state, const std::string& text) {
    DisplayLockGuard lock(this);
    task_state_ = state;
    int64_t now = esp_timer_get_time();
    notification_.clear();
    esp_timer_stop(shuttle_notification_timer_);
    if (state == "thinking") {
        sticky_until_us_ = now + kStickyBusyUs;
        acting_steps_ = 0;
        bool recognising = text.rfind("识别", 0) == 0;
        ui_.SetMood(ShuttleUi::Mood::kThinking, nullptr, recognising ? text.c_str() : "");
        if (!recognising && !text.empty()) {
            SetMessage("「" + text + "」");
        }
        return;
    }
    if (state == "acting") {
        sticky_until_us_ = now + kStickyBusyUs;
        acting_steps_++;
        std::string step = "第 " + std::to_string(acting_steps_) + " 步";
        ui_.SetMood(ShuttleUi::Mood::kActing, nullptr, step.c_str());
        ui_.SetSubtitle(step);
    } else if (state == "done") {
        sticky_until_us_ = now + kStickyDoneUs;
        ui_.SetMood(ShuttleUi::Mood::kDone, nullptr, "");
    } else if (state == "error") {
        sticky_until_us_ = now + kStickyDoneUs;
        ui_.SetMood(ShuttleUi::Mood::kError, nullptr, "");
    } else {  // idle / unknown
        sticky_until_us_ = 0;
        task_state_.clear();
        SetMessage("");
        ShowIdle();
        return;
    }
    if (!text.empty()) {
        SetMessage(text);
    }
    if ((state == "done" || state == "error") && partial_refreshes_ >= kFullRefreshAfterTaskPartials) {
        full_refresh_pending_ = true;
    }
}

void ShuttleDisplay::SetPhoneLink(bool ok, const std::string& detail) {
    DisplayLockGuard lock(this);
    phone_ok_ = ok;
    phone_detail_ = detail;
    ui_.SetPhoneConnected(ok);
    auto mood = ui_.mood();
    if (mood == ShuttleUi::Mood::kIdle || mood == ShuttleUi::Mood::kOffline) {
        ShowIdle();
    } else if (mood == ShuttleUi::Mood::kSleep) {
        ui_.SetSubtitle(ok ? "手机已连接" : detail);
    }
}

void ShuttleDisplay::SetServerLink(bool ok) {
    DisplayLockGuard lock(this);
    ui_.SetServerConnected(ok);
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
    if (!self->full_refresh_pending_ || self->ui_.animating()) {
        return;  // never flash the panel in the middle of a mascot animation
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
