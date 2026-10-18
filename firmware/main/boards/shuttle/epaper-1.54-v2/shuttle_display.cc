#include "shuttle_display.h"

#include <esp_log.h>
#include <esp_lvgl_port.h>

#include <cstdint>
#include <cstring>

#include "application.h"
#include "assets/lang_config.h"
#include "board.h"
#include "lvgl_theme.h"

#define TAG "ShuttleDisplay"

LV_FONT_DECLARE(font_noto_sans_basic_14_1);
LV_FONT_DECLARE(font_material_symbols_14_1);

namespace {

using Mood = ShuttleUi::Mood;

// Ghost-clearing policy for the partial-refresh waveform: a full refresh (the whole panel
// blinks for ~2 s) is *due* after many partial ones (or 30 min), and runs once the device has
// been quiet for kQuietUs. 40 partials / 5 s meant a blink about every minute of use.
constexpr int kFullRefreshEveryPartials = 150;
constexpr int64_t kFullRefreshIdleUs = 30LL * 60 * 1000 * 1000;
constexpr int64_t kQuietUs = 30LL * 1000 * 1000;
constexpr uint32_t kRefreshTimerMs = 200;

constexpr int64_t kStickyDoneUs = 60LL * 1000 * 1000;
constexpr int64_t kStickyErrorUs = 30LL * 1000 * 1000;
constexpr int64_t kStickyCancelUs = 10LL * 1000 * 1000;
constexpr int64_t kStickyBusyUs = 5LL * 60 * 1000 * 1000;  // server died mid-task: give up
constexpr int64_t kAwaitResultUs = 15LL * 1000 * 1000;
constexpr int64_t kDetailsUs = 10LL * 1000 * 1000;

constexpr const char* kIdleHint = "按住灯泡键说话";
constexpr const char* kWaiting = "……";

lv_obj_t* HiddenLabel(lv_obj_t* parent) {
    lv_obj_t* l = lv_label_create(parent);
    lv_label_set_text(l, "");
    lv_obj_add_flag(l, LV_OBJ_FLAG_HIDDEN);
    return l;
}

bool IsClock(const char* s) {
    // "HH:MM" from LvglDisplay::UpdateStatusBar while idle - this UI has no clock.
    return s != nullptr && strlen(s) == 5 && s[2] == ':' && s[0] >= '0' && s[0] <= '9';
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
    // Text: theme font (the assets partition swaps in the CJK "common" font). Status row:
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
    SetMood(Mood::kConnecting);
    ui_.SetContext(kWaiting);

    // Objects the upstream LvglDisplay code writes to. The Wi-Fi icon goes straight to the UI;
    // the others are hidden (battery is drawn by ShuttleUi, notifications by this class).
    network_label_ = ui_.wifi_label();
    mute_label_ = HiddenLabel(screen);
    battery_label_ = HiddenLabel(screen);
    status_label_ = HiddenLabel(screen);
    notification_label_ = HiddenLabel(screen);

    lv_display_add_event_cb(display_, OnFlushStart, LV_EVENT_FLUSH_START, this);
#if CONFIG_SHUTTLE_DEBUG_SCREEN_DUMP
    lv_display_add_event_cb(display_, OnFlushFinish, LV_EVENT_FLUSH_FINISH, this);
#endif
    refresh_timer_ = lv_timer_create(OnRefreshTimer, kRefreshTimerMs, this);
}

#if CONFIG_SHUTTLE_DEBUG_SCREEN_DUMP
void ShuttleDisplay::OnFlushFinish(lv_event_t* e) {
    static_cast<ShuttleDisplay*>(lv_event_get_user_data(e))->DumpFrame();
}
#endif

#if CONFIG_SHUTTLE_DEBUG_SCREEN_DUMP || CONFIG_SHUTTLE_DEBUG_CONSOLE
void ShuttleDisplay::DumpScreen() {
    DisplayLockGuard lock(this);
    DumpFrame();
}

void ShuttleDisplay::DumpFrame() {
    // Frame as the panel shows it (LVGL lock held): same threshold as the Waveshare flush
    // callback, packed 1 bpp MSB first (1 = black), base64, logged in chunks.
    // tools/screen_dump.py decodes it.
    lv_draw_buf_t* buf = lv_display_get_buf_active(display_);
    if (buf == nullptr || buf->data == nullptr) {
        return;
    }
    const int w = width_, h = height_;
    const uint32_t stride = buf->header.stride;
    static uint8_t bits[200 * 200 / 8];
    static char b64[(sizeof(bits) + 2) / 3 * 4 + 1];
    if ((size_t)(w * h / 8) > sizeof(bits)) {
        return;
    }
    memset(bits, 0, sizeof(bits));
    for (int y = 0; y < h; y++) {
        const uint16_t* row = reinterpret_cast<const uint16_t*>(buf->data + y * stride);
        for (int x = 0; x < w; x++) {
            if (row[x] < 0x7fff) {
                int i = y * w + x;
                bits[i >> 3] |= 0x80 >> (i & 7);
            }
        }
    }
    static const char* kAlphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    size_t n = w * h / 8, o = 0;
    for (size_t i = 0; i < n; i += 3) {
        uint32_t v = bits[i] << 16 | (i + 1 < n ? bits[i + 1] << 8 : 0) | (i + 2 < n ? bits[i + 2] : 0);
        b64[o++] = kAlphabet[(v >> 18) & 63];
        b64[o++] = kAlphabet[(v >> 12) & 63];
        b64[o++] = i + 1 < n ? kAlphabet[(v >> 6) & 63] : '=';
        b64[o++] = i + 2 < n ? kAlphabet[v & 63] : '=';
    }
    b64[o] = '\0';
    const size_t kChunk = 1000;
    uint32_t seq = ++dump_seq_;
    int parts = (o + kChunk - 1) / kChunk;
    for (int p = 0; p < parts; p++) {
        ESP_LOGI(TAG, "SCRDUMP %lu %dx%d %d/%d %.*s", (unsigned long)seq, w, h, p + 1, parts, (int)kChunk,
                 b64 + p * kChunk);
    }
}
#endif

void ShuttleDisplay::SetTheme(Theme* theme) {
    // Black/white panel: ignore theme colours, but adopt its (possibly new) text font.
    DisplayLockGuard lock(this);
    current_theme_ = theme;
    if (setup_ui_called_) {
        ui_.SetFonts(CurrentFonts());
    }
}

bool ShuttleDisplay::StickyActive() const { return esp_timer_get_time() < sticky_until_us_.load(); }

void ShuttleDisplay::NoteActivity() { last_activity_us_ = esp_timer_get_time(); }

void ShuttleDisplay::SetMood(Mood mood) {
    if (mood_.load() != static_cast<int>(mood)) {
        static const char* kNames[] = {"idle",   "sleep",    "connecting", "listening", "thinking",
                                       "acting", "speaking", "done",       "error",     "offline"};
        ESP_LOGI(TAG, "Mood: %s", kNames[static_cast<int>(mood)]);
    }
    ui_.SetMood(mood);
    mood_ = static_cast<int>(mood);
}

void ShuttleDisplay::SetMessage(const std::string& text) {
    message_ = text;
    if (notification_.empty()) {
        ui_.SetMessage(message_);
    }
}

void ShuttleDisplay::ShowIdle() {
    // Octopus at rest + how to talk. The last task result stays in the message area until the
    // next command (handy to re-read it); a phone problem replaces it with what to do; anything
    // else (boot steps, Wi-Fi notices) is cleared.
    bool had_result = task_state_ == "done" || task_state_ == "error" || task_state_ == "cancelled";
    task_state_.clear();
    bool problem = !phone_ok_ && !phone_problem_.empty();
    SetMood(problem ? Mood::kOffline : Mood::kIdle);
    ui_.SetContext(kIdleHint);
    if (problem) {
        SetMessage(phone_problem_);
    } else if (!had_result) {
        SetMessage("");
    }
}

void ShuttleDisplay::SetStatus(const char* status) {
    if (status == nullptr || IsClock(status)) {
        return;
    }
    // Set from the main loop right before xiaozhi opens the channel, starts the microphone or
    // resets its decoder: waiting for the LVGL lock there (a refresh holds it up to ~2 s) cut
    // off the first words / the start of the reply. Applied now if the lock is free, else by
    // the refresh timer (OnRefreshTimer).
    int pending = kPendingNone;
    if (strcmp(status, Lang::Strings::CONNECTING) == 0) {
        pending = kPendingConnecting;
    } else if (strcmp(status, Lang::Strings::LISTENING) == 0) {
        pending = kPendingListening;
    } else if (strcmp(status, Lang::Strings::SPEAKING) == 0) {
        pending = kPendingSpeaking;
    }
    if (pending != kPendingNone) {
        if (TryLock()) {
            pending_status_ = kPendingNone;
            ApplyStatus(pending);
            Unlock();
        } else {
            pending_status_ = pending;
        }
        return;
    }
    if (strcmp(status, Lang::Strings::STANDBY) == 0 && awaiting_result_.load() && StickyActive()) {
        return;  // PTT just released: keep "thinking" until the server answers
    }
    DisplayLockGuard lock(this);
    ESP_LOGI(TAG, "Status: %s", status);
    NoteActivity();
    pending_status_ = kPendingNone;  // superseded
    if (strcmp(status, Lang::Strings::STANDBY) == 0) {
        awaiting_result_ = false;
        if (task_state_ == "recognizing" || task_state_ == "thinking" || task_state_ == "acting") {
            // Standby in the middle of a task = the channel closed (server restarted, network
            // lost): the result will never come. Say so instead of "acting" for 5 minutes.
            ESP_LOGW(TAG, "Channel closed during task (%s)", task_state_.c_str());
            task_state_ = "error";
            sticky_until_us_ = esp_timer_get_time() + kStickyErrorUs;
            SetMood(Mood::kError);
            SetMessage("连接断开，任务没有完成");
            return;
        }
        if (!StickyActive()) {
            // Ready: drop boot-time notices (xiaozhi shows "已连接 <ssid>" for 30 s).
            notification_.clear();
            esp_timer_stop(shuttle_notification_timer_);
            ShowIdle();
            ui_.SetMessage(message_);
        } else if (task_state_ == "done") {
            SetMood(Mood::kDone);  // TTS finished: back from the talking mouth to the result
        } else if (task_state_ == "error") {
            SetMood(Mood::kError);
        }
    } else if (strcmp(status, Lang::Strings::ERROR) == 0) {
        SetMood(Mood::kError);
        ui_.SetContext("");
    } else if (strcmp(status, Lang::Strings::WIFI_CONFIG_MODE) == 0) {
        SetMood(Mood::kConnecting);
        ui_.SetContext("Wi-Fi 配网");  // the message (from xiaozhi) says which hotspot to join
    } else if (strcmp(status, Lang::Strings::OTA_UPGRADE) == 0) {
        SetMood(Mood::kThinking);
        ui_.SetContext("升级固件");
    } else {
        // Boot/activation steps ("检查新版本...", "登录服务器..."): the step goes to the message.
        if (mood_.load() == static_cast<int>(Mood::kConnecting)) {
            SetMessage(status);
        }
    }
}

void ShuttleDisplay::ApplyStatus(int pending) {
    // LVGL lock held.
    NoteActivity();
    if (pending == kPendingConnecting) {
        ESP_LOGI(TAG, "Status: connecting");
        sticky_until_us_ = 0;
        SetMood(Mood::kConnecting);
        ui_.SetContext(kWaiting);
    } else if (pending == kPendingListening) {
        ESP_LOGI(TAG, "Status: listening");
        sticky_until_us_ = 0;
        awaiting_result_ = false;
        task_state_.clear();
        SetMood(Mood::kListening);
        ui_.SetContext("松开发送");
        SetMessage("");
    } else if (pending == kPendingSpeaking) {
        SetMood(Mood::kSpeaking);
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
    if (strncmp(notification, Lang::Strings::VERSION, strlen(Lang::Strings::VERSION)) == 0) {
        return;  // "版本 x.y.z" after activation: on the details page instead
    }
    if (StickyActive()) {
        return;  // do not hide the task result behind e.g. "已连接 xxx"
    }
    notification_ = notification;
    ui_.SetMessage(notification_);
    esp_timer_stop(shuttle_notification_timer_);
    notification_expired_ = false;
    esp_timer_start_once(shuttle_notification_timer_, static_cast<uint64_t>(duration_ms) * 1000);
}

void ShuttleDisplay::OnNotificationTimer(void* arg) {
    // esp_timer task, which also polls the keys: never wait for the LVGL lock here. The
    // refresh timer restores the message area.
    static_cast<ShuttleDisplay*>(arg)->notification_expired_ = true;
}

void ShuttleDisplay::SetEmotion(const char* emotion) {
    // The mascot's mood follows device/task state, not xiaozhi's emoji names.
}

void ShuttleDisplay::SetChatMessage(const char* role, const char* content) {
    bool is_assistant = role != nullptr && strcmp(role, "assistant") == 0;
    if (is_assistant && mood_.load() != static_cast<int>(Mood::kIdle)) {
        return;  // TTS subtitle of a Shuttle task: SetTaskState already shows the reply
    }
    if (content == nullptr || content[0] == '\0') {
        // Clears come from the main loop on state changes (e.g. connecting): never wait for a
        // refresh for them, the states that matter clear the message area themselves.
        if (TryLock()) {
            if (!StickyActive()) {
                SetMessage("");
            }
            Unlock();
        }
        return;
    }
    DisplayLockGuard lock(this);
    if (role != nullptr && strcmp(role, "user") == 0) {
        ui_.SetContext(content);  // xiaozhi "stt": the recognised instruction
    } else {
        SetMessage(content);
    }
}

void ShuttleDisplay::ClearChatMessages() {
    if (StickyActive()) {
        return;
    }
    DisplayLockGuard lock(this);
    SetMessage("");
}

void ShuttleDisplay::UpdateStatusBar(bool update_all) {
    // Main loop, every second. While an e-paper refresh holds the LVGL lock, skip the tick
    // instead of waiting: the main loop would also sit on a push-to-talk press meanwhile.
    // (The lock is recursive: the guards below and upstream's re-enter it.)
    if (!(update_all ? Lock(30000) : TryLock())) {
        return;
    }
    // Upstream logic: Wi-Fi icon (-> ui_.wifi_label()), low battery popup.
    LvglDisplay::UpdateStatusBar(update_all);

    // Battery in 5% steps with hysteresis, so ADC noise around a step boundary does not
    // refresh the panel (95% -> 90% -> 95% ...).
    int level = 0;
    bool charging = false, discharging = false;
    if (Board::GetInstance().GetBatteryLevel(level, charging, discharging)) {
        int q = (level + 2) / 5 * 5;
        bool moved = last_battery_pct_ < 0 || level >= last_battery_pct_ + 5 || level <= last_battery_pct_ - 3;
        if (update_all || (q != last_battery_pct_ && moved)) {
            last_battery_pct_ = q;
            DisplayLockGuard lock(this);
            ui_.SetBattery(q, charging);
        }
    }

    // Task result shown long enough (or the server never answered / died mid-task): idle.
    int mood = mood_.load();
    bool task_mood = mood == (int)Mood::kThinking || mood == (int)Mood::kActing || mood == (int)Mood::kDone ||
                     mood == (int)Mood::kError || mood == (int)Mood::kSpeaking;
    if (task_mood && !StickyActive() && task_state_ != "off" &&
        Application::GetInstance().GetDeviceState() == kDeviceStateIdle) {
        DisplayLockGuard lock(this);
        awaiting_result_ = false;
        ShowIdle();
    }

    int64_t now = esp_timer_get_time();
    if (partial_refreshes_ > 0 && now - last_full_refresh_us_ > kFullRefreshIdleUs) {
        full_refresh_due_ = true;
    }
    Unlock();
}

void ShuttleDisplay::OnUtteranceSent() {
    // PTT tail timer (esp_timer task, which also polls the keys): flags now, so the coming
    // "standby" keeps the screen; "thinking" is drawn by the refresh timer.
    NoteActivity();
    sticky_until_us_ = esp_timer_get_time() + kAwaitResultUs;
    awaiting_result_ = true;
    utterance_sent_ = true;
}

bool ShuttleDisplay::TaskBusy() const {
    int mood = mood_.load();
    return (awaiting_result_.load() || mood == (int)Mood::kThinking || mood == (int)Mood::kActing) &&
           StickyActive();
}

void ShuttleDisplay::SetTaskState(const std::string& state, const std::string& text) {
    DisplayLockGuard lock(this);
    NoteActivity();
    awaiting_result_ = false;
    task_state_ = state;
    int64_t now = esp_timer_get_time();
    notification_.clear();
    esp_timer_stop(shuttle_notification_timer_);
    notification_expired_ = false;
    utterance_sent_ = false;
    if (state == "recognizing") {
        sticky_until_us_ = now + kStickyBusyUs;
        SetMood(Mood::kThinking);
        ui_.SetContext(kWaiting);
        SetMessage("");
    } else if (state == "thinking") {
        sticky_until_us_ = now + kStickyBusyUs;
        SetMood(Mood::kThinking);
        if (!text.empty()) {
            ui_.SetContext(text);  // what the user said
        }
        SetMessage("");
    } else if (state == "acting") {
        sticky_until_us_ = now + kStickyBusyUs;
        SetMood(Mood::kActing);
        SetMessage(text);  // "第 2 步：点击 搜索框"
    } else if (state == "done") {
        sticky_until_us_ = now + kStickyDoneUs;
        SetMood(Mood::kDone);
        SetMessage(text);
    } else if (state == "error") {
        sticky_until_us_ = now + kStickyErrorUs;
        SetMood(Mood::kError);
        SetMessage(text);
    } else if (state == "cancelled") {
        sticky_until_us_ = now + kStickyCancelUs;
        SetMood(Mood::kIdle);
        ui_.SetContext(kIdleHint);
        SetMessage(text.empty() ? "已取消" : text);
    } else if (state == "off") {
        sticky_until_us_ = INT64_MAX;
        SetMood(Mood::kSleep);
        ui_.SetContext("");
        SetMessage(text);
    } else {  // idle / unknown (e.g. the press was too short)
        sticky_until_us_ = 0;
        ShowIdle();
    }
}

// The link setters run on the main loop (e.g. when the channel opens, right before the
// microphone starts), so they never wait for a refresh.
void ShuttleDisplay::SetPhoneLink(bool ok, const std::string& problem) {
    RunOrDefer([this, ok, problem]() {
        bool changed = ok != phone_ok_ || problem != phone_problem_;
        phone_ok_ = ok;
        phone_problem_ = ok ? "" : problem;
        ui_.SetPhoneConnected(ok);
        int mood = mood_.load();
        if (changed && (mood == (int)Mood::kIdle || mood == (int)Mood::kOffline || mood == (int)Mood::kSleep) &&
            !StickyActive()) {
            if (ok && !message_.empty() && message_ == problem) {
                SetMessage("");
            }
            ShowIdle();
        }
    });
}

void ShuttleDisplay::SetServerLink(int state) {
    // The cloud-off icon only appears when the server is known to be unreachable.
    RunOrDefer([this, state]() { ui_.SetServerConnected(state != 0); });
}

void ShuttleDisplay::SetHomeNetwork(bool home) {
    RunOrDefer([this, home]() { ui_.SetHomeNetwork(home); });
}

void ShuttleDisplay::RunOrDefer(std::function<void()> fn) {
    {
        std::lock_guard<std::mutex> guard(deferred_mutex_);
        if (!deferred_.empty()) {
            deferred_.push_back(std::move(fn));  // behind the queued ones
            return;
        }
    }
    if (TryLock()) {
        fn();
        Unlock();
        return;
    }
    std::lock_guard<std::mutex> guard(deferred_mutex_);
    deferred_.push_back(std::move(fn));
}

void ShuttleDisplay::ShowDetails(const std::string& text) {
    DisplayLockGuard lock(this);
    if (!details_shown_) {
        saved_context_ = ui_.context();
        details_shown_ = true;
    }
    auto nl = text.find('\n');
    ui_.SetContext(nl == std::string::npos ? text : text.substr(0, nl));
    notification_ = nl == std::string::npos ? " " : text.substr(nl + 1);
    ui_.SetMessage(notification_);
    esp_timer_stop(shuttle_notification_timer_);
    notification_expired_ = false;
    esp_timer_start_once(shuttle_notification_timer_, kDetailsUs);
}

void ShuttleDisplay::RequestFullRefresh() { full_refresh_now_ = true; }

void ShuttleDisplay::OnFlushStart(lv_event_t* e) {
    auto* self = static_cast<ShuttleDisplay*>(lv_event_get_user_data(e));
    self->partial_refreshes_++;
    if (self->partial_refreshes_ >= kFullRefreshEveryPartials) {
        self->full_refresh_due_ = true;
    }
}

void ShuttleDisplay::OnRefreshTimer(lv_timer_t* timer) {
    // Runs inside the LVGL task with the port lock held, so no flush can interleave.
    auto* self = static_cast<ShuttleDisplay*>(lv_timer_get_user_data(timer));
    std::vector<std::function<void()>> deferred;
    {
        std::lock_guard<std::mutex> guard(self->deferred_mutex_);
        deferred.swap(self->deferred_);
    }
    for (auto& fn : deferred) {
        fn();
    }
    int pending = self->pending_status_.exchange(kPendingNone);
    if (pending != kPendingNone) {
        auto state = Application::GetInstance().GetDeviceState();
        if ((pending == kPendingConnecting && state == kDeviceStateConnecting) ||
            (pending == kPendingListening && state == kDeviceStateListening) ||
            (pending == kPendingSpeaking && state == kDeviceStateSpeaking)) {
            self->ApplyStatus(pending);
        }
    }
    if (self->utterance_sent_.exchange(false) && self->awaiting_result_.load()) {
        self->SetMood(Mood::kThinking);  // until the server reports (no flash of idle)
        self->ui_.SetContext(kWaiting);
        self->SetMessage("");
    }
    if (self->notification_expired_.exchange(false)) {
        self->notification_.clear();
        if (self->details_shown_) {
            self->details_shown_ = false;
            self->ui_.SetContext(self->saved_context_);
        }
        self->ui_.SetMessage(self->message_);
    }
    bool now_requested = self->full_refresh_now_;
    if (!now_requested) {
        if (!self->full_refresh_due_ || self->ui_.animating()) {
            return;  // never flash the panel in the middle of a mascot animation
        }
        bool quiet = esp_timer_get_time() - self->last_activity_us_.load() >= kQuietUs &&
                     !self->awaiting_result_.load() &&
                     Application::GetInstance().GetDeviceState() == kDeviceStateIdle;
        if (!quiet) {
            return;  // e.g. a reply is about to be spoken: the 2 s full refresh would cut it
        }
    }
    self->full_refresh_now_ = false;
    self->full_refresh_due_ = false;
    ESP_LOGI(TAG, "Full refresh (after %d partial refreshes)", self->partial_refreshes_);
    // The driver's frame buffer still holds the last rendered frame; push it with the
    // full waveform (clears ghosting), then re-arm the partial-refresh waveform.
    self->EPD_Init();
    self->EPD_DisplayPartBaseImage();
    self->EPD_Init_Partial();
    self->partial_refreshes_ = 0;
    self->last_full_refresh_us_ = esp_timer_get_time();
}
