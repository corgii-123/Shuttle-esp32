// Shuttle display: maps xiaozhi device states, server task status ("shuttle" messages), the
// phone/server links and touch actions onto ShuttleUi (the octopus mascot is the state, see
// shuttle_ui.h), and owns the e-paper refresh policy.
//
// The upstream Waveshare driver refreshes the panel with the partial waveform on every LVGL
// flush, holding the LVGL lock for ~0.3-0.5 s (a full, ghost-clearing refresh: ~2 s). Rules:
//   * labels are only touched when their text changes (ShuttleUi);
//   * the main loop never waits on that lock on the audio path: xiaozhi sets "connecting" /
//     "listening" right before it starts the microphone and "speaking" right before it resets
//     the decoder, so those states are applied only if the lock is free, else deferred to the
//     LVGL task; the 1 s status-bar tick is skipped while a refresh runs;
//   * full refreshes only run after the device has been idle for a few seconds (never right
//     after a task, when the reply is about to be spoken).
#pragma once

#include <esp_timer.h>
#include <sdkconfig.h>

#include <atomic>
#include <functional>
#include <mutex>
#include <string>
#include <vector>

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
    // Server task progress: recognizing | thinking | acting | done | error | cancelled | idle,
    // plus "off" (power-off screen, stays until reboot).
    void SetTaskState(const std::string& state, const std::string& text);
    // Phone reachability. ok=false with a non-empty problem shows it while idle (what to do).
    void SetPhoneLink(bool ok, const std::string& problem);
    // Server reachability: 1 = reachable, 0 = unreachable, -1 = unknown.
    void SetServerLink(int state);
    void SetHomeNetwork(bool home);  // phone reached via the router (house icon)
    // Push-to-talk released: show "thinking" until the server reports (avoids a flash of idle).
    void OnUtteranceSent();
    // A server task is running (recognizing / thinking / acting).
    bool TaskBusy() const;
    // Device details page for 10 s: first line in the bubble, the rest in the message area.
    void ShowDetails(const std::string& text);
    // Ghost-clearing full refresh now (user request, e.g. long press on the screen).
    void RequestFullRefresh();
#if CONFIG_SHUTTLE_DEBUG_SCREEN_DUMP || CONFIG_SHUTTLE_DEBUG_CONSOLE
    // Log the current frame (SCRDUMP lines, tools/screen_dump.py).
    void DumpScreen();
#endif

private:
    enum Pending { kPendingNone, kPendingConnecting, kPendingListening, kPendingSpeaking };

    static void OnFlushStart(lv_event_t* e);
#if CONFIG_SHUTTLE_DEBUG_SCREEN_DUMP
    static void OnFlushFinish(lv_event_t* e);
#endif
#if CONFIG_SHUTTLE_DEBUG_SCREEN_DUMP || CONFIG_SHUTTLE_DEBUG_CONSOLE
    void DumpFrame();
    uint32_t dump_seq_ = 0;
#endif
    static void OnRefreshTimer(lv_timer_t* timer);
    static void OnNotificationTimer(void* arg);

    ShuttleUi::Fonts CurrentFonts() const;
    bool TryLock() { return Lock(1); }  // 1 ms = 0 ticks: no wait (0 would wait forever)
    // Runs fn with the LVGL lock held: now if it is free, else from the refresh timer (in
    // order). For main-loop updates that must not wait for an e-paper refresh.
    void RunOrDefer(std::function<void()> fn);
    void ApplyStatus(int pending);
    void SetMood(ShuttleUi::Mood mood);
    void SetMessage(const std::string& text);
    void ShowIdle();
    bool StickyActive() const;
    void NoteActivity();

    ShuttleUi ui_;
    lv_timer_t* refresh_timer_ = nullptr;
    esp_timer_handle_t shuttle_notification_timer_ = nullptr;

    std::string task_state_;       // last server task state
    std::string message_;          // message area text (without notification)
    std::string notification_;     // temporary text shown instead of message_
    std::string phone_problem_;    // idle hint when the phone is not reachable
    std::string saved_context_;    // bubble text hidden by the details page
    bool details_shown_ = false;
    bool phone_ok_ = false;
    int last_battery_pct_ = -100;

    std::atomic<int> mood_{-1};                  // mirror of ui_.mood(), readable without the lock
    std::atomic<int> pending_status_{kPendingNone};  // deferred SetStatus (see ApplyStatus)
    std::atomic<bool> utterance_sent_{false};        // draw "thinking" (OnUtteranceSent)
    std::atomic<bool> notification_expired_{false};  // restore the message area
    std::mutex deferred_mutex_;
    std::vector<std::function<void()>> deferred_;  // RunOrDefer queue
    std::atomic<bool> awaiting_result_{false};   // utterance sent, waiting for the server
    std::atomic<int64_t> sticky_until_us_{0};    // keep the task result until this time
    std::atomic<int64_t> last_activity_us_{0};

    int partial_refreshes_ = 0;
    int64_t last_full_refresh_us_ = 0;
    volatile bool full_refresh_due_ = false;  // ghosts accumulated: refresh once idle
    volatile bool full_refresh_now_ = false;  // user asked for it
};
