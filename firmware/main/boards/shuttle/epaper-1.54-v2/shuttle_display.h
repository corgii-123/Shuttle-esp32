// Shuttle UI for the 200x200 black/white e-paper.
//
// Layout (y ranges in px):
//   0-26    top bar   : Wi-Fi icon, phone icon + state, clock, battery % + icon
//   30-62   state bar : big inverted label: 连接中 / 录音中 / 思考中 / 执行中 / 完成 / 错误 ...
//   66-170  message   : user words / agent progress / final reply (wrapped, never scrolls)
//   174-200 bottom bar: phone (ADB) link detail + server link
//
// The e-paper is refreshed by the upstream Waveshare driver with the partial-refresh
// waveform on every LVGL flush. To keep refreshes rare, labels are only touched when their
// text actually changes, nothing scrolls/animates, and a full (ghost-clearing) refresh is
// done every N partial refreshes or when the device returns to idle after a task.
#pragma once

#include <esp_timer.h>

#include <string>

#include "../../waveshare/esp32-s3-epaper-1.54/custom_lcd_display.h"

class ShuttleDisplay : public CustomLcdDisplay {
public:
    ShuttleDisplay(int width, int height, custom_lcd_spi_t spi);
    ~ShuttleDisplay();

    // xiaozhi Display interface
    void SetupUI() override;
    void SetStatus(const char* status) override;
    void ShowNotification(const char* notification, int duration_ms = 3000) override;
    void ShowNotification(const std::string& notification, int duration_ms = 3000) override;
    void SetEmotion(const char* emotion) override;
    void SetChatMessage(const char* role, const char* content) override;
    void ClearChatMessages() override;
    void SetTheme(Theme* theme) override;
    void UpdateStatusBar(bool update_all = false) override;
    void SetPreviewImage(std::unique_ptr<LvglImage> image) override {}
    void SetHideSubtitle(bool hide) override {}

    // Shuttle extensions (thread-safe)
    // state: thinking | acting | done | error | idle (server task progress)
    void SetTaskState(const std::string& state, const std::string& text);
    // Phone / ADB link line, e.g. icon_ok=true, "手机已连接"
    void SetPhoneLink(bool ok, const std::string& detail);
    void SetServerLink(bool ok);
    // Schedule a ghost-clearing full refresh at the next LVGL tick.
    void RequestFullRefresh();

private:
    static void OnFlushStart(lv_event_t* e);
    static void OnRefreshTimer(lv_timer_t* timer);
    static void OnNotificationTimer(void* arg);

    void SetLabelText(lv_obj_t* label, const std::string& text);
    void SetStateText(const std::string& text);
    void SetMessageText(const std::string& text);
    void ApplyFonts();
    bool StickyActive() const;

    lv_obj_t* top_bar_ = nullptr;
    lv_obj_t* phone_icon_ = nullptr;
    lv_obj_t* clock_label_ = nullptr;
    lv_obj_t* battery_pct_label_ = nullptr;
    lv_obj_t* state_bar_ = nullptr;
    lv_obj_t* state_label_ = nullptr;
    lv_obj_t* message_label_ = nullptr;
    lv_obj_t* bottom_bar_ = nullptr;
    lv_obj_t* link_label_ = nullptr;
    lv_obj_t* server_icon_ = nullptr;

    lv_timer_t* refresh_timer_ = nullptr;
    esp_timer_handle_t shuttle_notification_timer_ = nullptr;

    std::string task_state_;       // last server task state
    std::string message_;          // current message area text (without notification)
    std::string notification_;     // temporary notification shown instead of message_
    int64_t sticky_until_us_ = 0;  // keep task state/message on idle until this time
    int last_battery_pct_ = -100;
    int partial_refreshes_ = 0;
    int64_t last_full_refresh_us_ = 0;
    volatile bool full_refresh_pending_ = false;
};
