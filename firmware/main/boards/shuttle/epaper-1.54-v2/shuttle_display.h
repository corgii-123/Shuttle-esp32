// Shuttle e-paper display: xiaozhi Display interface -> ShuttleUi (mascot UI, shuttle_ui.h).
//
// This class only *maps* events to the UI:
//   xiaozhi device state (SetStatus)     -> mascot mood (连接中 / 录音中 / 播报中 / 待命 ...)
//   server task status (SetTaskState)    -> 思考中 / 执行中 / 完成 / 出错了 + message
//   ADB bridge / server link             -> status-bar icons + idle subtitle
// and owns the e-paper refresh policy (partial refresh on every LVGL flush by the upstream
// Waveshare driver; a ghost-clearing full refresh every N partials, never mid-animation).
#pragma once

#include <esp_timer.h>

#include <string>

#include "../../waveshare/esp32-s3-epaper-1.54/custom_lcd_display.h"
#include "shuttle_ui.h"

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
    void SetPhoneLink(bool ok, const std::string& detail);
    void SetServerLink(bool ok);
    void SetHomeNetwork(bool home);  // home icon: phone reached through the router (fixed IP)
    void RequestFullRefresh();

private:
    static void OnFlushStart(lv_event_t* e);
    static void OnRefreshTimer(lv_timer_t* timer);
    static void OnNotificationTimer(void* arg);

    ShuttleUi::Fonts CurrentFonts() const;
    void ShowIdle();  // 待命 (or offline mascot when the phone link is down)
    void SetMessage(const std::string& text);
    bool StickyActive() const;

    ShuttleUi ui_;
    lv_timer_t* refresh_timer_ = nullptr;
    esp_timer_handle_t shuttle_notification_timer_ = nullptr;

    std::string task_state_;      // last server task state
    std::string message_;         // message area text (without notification)
    std::string notification_;    // temporary notification shown instead of message_
    int64_t sticky_until_us_ = 0; // keep the task result on screen until this time
    int acting_steps_ = 0;
    bool hint_shown_ = false;     // message_ currently holds an idle hint
    bool phone_ok_ = false;
    std::string phone_detail_ = "手机未连接";
    int last_battery_pct_ = -100;
    int partial_refreshes_ = 0;
    int64_t last_full_refresh_us_ = 0;
    volatile bool full_refresh_pending_ = false;
};
