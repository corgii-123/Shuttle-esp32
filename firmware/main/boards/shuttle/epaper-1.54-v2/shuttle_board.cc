// Shuttle board: Waveshare ESP32-S3-ePaper-1.54 V2 + push-to-talk + ADB bridge.
//
// Derived from main/boards/waveshare/esp32-s3-epaper-1.54/waveshare-s3-epaper-1.54.cc.
// Differences from upstream (everything else, incl. the PWR key power behaviour, is kept):
//   * BOOT (light-bulb) key is push-to-talk (hold to record, release to send).
//   * Custom e-paper UI (ShuttleDisplay/ShuttleUi: octopus mascot) + FT6336 touch: tap =
//     cancel the task / device details, long press = ghost-clearing full refresh.
//   * Wi-Fi uses ShuttleWifi (priority order + roam-up) and never falls into the config
//     portal just because the hotspot is off; double-click PWR opens the portal.
//   * Starts the ADB TCP<->WebSocket bridge; phones are found by scanning the Wi-Fi for
//     adbd (the hotspot phone or phones on a router) and the server picks which one.
//   * Handles "shuttle" JSON messages from the server (task progress, phone state).
#include <driver/gpio.h>
#include <driver/i2c_master.h>
#include <esp_adc/adc_cali.h>
#include <esp_adc/adc_cali_scheme.h>
#include <esp_adc/adc_oneshot.h>
#include <esp_log.h>
#include <esp_mac.h>
#include <esp_app_desc.h>
#include <esp_system.h>
#include <esp_timer.h>
#include <esp_wifi.h>

#include <cJSON.h>

#include <atomic>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <mutex>

#include "application.h"
#include "assets/lang_config.h"
#include "button.h"
#include "codecs/es8311_audio_codec.h"
#include "config.h"
#include "mcp_server.h"
#include "system_info.h"
#include "wifi_board.h"

#include "../../waveshare/esp32-s3-epaper-1.54/board_power_bsp.h"
#include "shuttle/adb_bridge.h"
#include "shuttle/debug_console.h"
#include "shuttle/shuttle_config.h"
#include "shuttle_display.h"
#include "shuttle_touch.h"
#include "shuttle_wifi.h"

#define TAG "ShuttleBoard"

// ES8311 without its power-up click. xiaozhi powers the ADC down after 15 s without recording;
// switching it back on puts a ~50 ms full-scale click at the start of the next recording, right
// where the first syllable is (ASR heard "嗯" in silent presses). Upstream's 120 ms warm-up runs
// before the codec is enabled, so it does not skip it: mute it here instead.
class ShuttleAudioCodec : public Es8311AudioCodec {
public:
    using Es8311AudioCodec::Es8311AudioCodec;

    void EnableInput(bool enable) override {
        if (enable && !input_enabled()) {
            int per_ms = input_sample_rate() / 1000 * input_channels();
            mute_left_ = kMuteMs * per_ms;
            fade_len_ = fade_left_ = kFadeMs * per_ms;
        }
        Es8311AudioCodec::EnableInput(enable);
    }

    bool InputData(std::vector<int16_t>& data) override {
        if (!AudioCodec::InputData(data)) {
            return false;
        }
        size_t i = 0;
        for (; i < data.size() && mute_left_ > 0; i++, mute_left_--) {
            data[i] = 0;
        }
        for (; i < data.size() && fade_left_ > 0; i++, fade_left_--) {
            data[i] = static_cast<int16_t>(data[i] * (fade_len_ - fade_left_) / fade_len_);
        }
        return true;
    }

private:
    static constexpr int kMuteMs = 60;
    static constexpr int kFadeMs = 20;
    int mute_left_ = 0;
    int fade_left_ = 0;
    int fade_len_ = 1;
};

class ShuttleBoard : public WifiBoard {
private:
    i2c_master_bus_handle_t i2c_bus_ = nullptr;
    Button boot_button_;
    Button pwr_button_;
    ShuttleDisplay* display_ = nullptr;
    BoardPowerBsp* power_ = nullptr;
    std::unique_ptr<shuttle::AdbBridge> bridge_;
    ShuttleTouch touch_;

    std::atomic<bool> ptt_held_{false};
    esp_timer_handle_t ptt_release_timer_ = nullptr;
    int ptt_release_polls_ = 0;
    int64_t ptt_release_us_ = 0;
    esp_timer_handle_t console_ptt_timer_ = nullptr;  // debug console "ptt <ms>" release

    // Wi-Fi power save, see ApplyPowerSave()
    std::mutex ps_mutex_;
    PowerSaveLevel requested_ps_ = PowerSaveLevel::LOW_POWER;  // what xiaozhi asks for
    PowerSaveLevel applied_ps_ = PowerSaveLevel::LOW_POWER;
    bool ps_applied_ = false;
    std::atomic<bool> adb_busy_{false};
    uint64_t ps_bytes_ = 0;
    int64_t ps_traffic_us_ = 0;
    esp_timer_handle_t ps_timer_ = nullptr;

    std::string i2c_devices_;       // boot-time I2C scan, logged again once Wi-Fi is up
    std::string server_adb_state_;  // from server: device / unauthorized / offline / ...
    std::string server_phone_link_; // from server: esp32 / mac / none ("" = not reported yet)
    bool server_seen_ = false;      // got any Shuttle message from the server
    bool home_network_ = false;     // the phone in use is not the hotspot (reached via a router)
    std::string phone_name_;        // from the ADB bridge ("Redmi K30 Pro"), for the details page
    uint64_t last_bridge_bytes_ = 0;
    int64_t last_bridge_activity_us_ = 0;

    void InitializeI2c() {
        i2c_master_bus_config_t i2c_bus_cfg = {};
        i2c_bus_cfg.i2c_port = (i2c_port_t)0;
        i2c_bus_cfg.sda_io_num = AUDIO_CODEC_I2C_SDA_PIN;
        i2c_bus_cfg.scl_io_num = AUDIO_CODEC_I2C_SCL_PIN;
        i2c_bus_cfg.clk_source = I2C_CLK_SRC_DEFAULT;
        i2c_bus_cfg.glitch_ignore_cnt = 7;
        i2c_bus_cfg.intr_priority = 0;
        i2c_bus_cfg.trans_queue_depth = 0;
        i2c_bus_cfg.flags.enable_internal_pullup = 1;
        ESP_ERROR_CHECK(i2c_new_master_bus(&i2c_bus_cfg, &i2c_bus_));
        ScanI2c();
    }

    // Log what answers on the codec I2C bus (hardware variants differ: RTC, sensor, touch).
    void ScanI2c() {
        std::string found;
        for (uint16_t addr = 0x08; addr < 0x78; addr++) {
            if (i2c_master_probe(i2c_bus_, addr, 20) != ESP_OK) {
                continue;
            }
            const char* name = "?";
            switch (addr) {
                case 0x18: name = "ES8311"; break;
                case 0x51: name = "PCF85063 RTC"; break;
                case 0x70: name = "SHTC3"; break;
                case 0x15: name = "CST816 touch"; break;
                case 0x38: name = "FT6x36 touch"; break;
                case 0x14:
                case 0x5D: name = "GT911/GT1151 touch"; break;
                case 0x48: name = "touch?"; break;
            }
            char buf[40];
            snprintf(buf, sizeof(buf), "%s0x%02X(%s)", found.empty() ? "" : " ", addr, name);
            found += buf;
        }
        i2c_devices_ = found.empty() ? "none" : found;
        ESP_LOGI(TAG, "I2C devices: %s", i2c_devices_.c_str());
    }

    // ---- BOOT (light-bulb) key: push-to-talk -----------------------------------------
    // Listening starts on press: microphone start-up already takes ~0.3 s, and a hold delay
    // (to free the key for click gestures) cut off the first word or two. Recording stops
    // CONFIG_SHUTTLE_PTT_TAIL_MS after release. Gestures live on the PWR key instead.

    void StartReleaseTail() {
        ptt_release_us_ = esp_timer_get_time();
        ptt_release_polls_ = 0;
        esp_timer_stop(ptt_release_timer_);
        esp_timer_start_periodic(ptt_release_timer_, 100 * 1000);
    }

    static void OnPttReleaseTimer(void* arg) {
        // Runs every 100 ms after the key is released. Keeps recording for
        // CONFIG_SHUTTLE_PTT_TAIL_MS (the last syllable is still in the audio pipeline when
        // the finger lifts), and handles a release while the channel is still connecting
        // (Connecting state ignores StopListening): keep trying until we leave it.
        auto* self = static_cast<ShuttleBoard*>(arg);
        auto& app = Application::GetInstance();
        auto state = app.GetDeviceState();
        if (self->ptt_held_) {
            esp_timer_stop(self->ptt_release_timer_);  // pressed again: keep listening
            return;
        }
        if (state == kDeviceStateListening) {
            if (esp_timer_get_time() - self->ptt_release_us_ < CONFIG_SHUTTLE_PTT_TAIL_MS * 1000LL) {
                return;
            }
            ESP_LOGI(TAG, "PTT tail done -> stop listening");
            self->display_->OnUtteranceSent();  // first: the coming "standby" keeps the screen
            app.StopListening();
            esp_timer_stop(self->ptt_release_timer_);
        } else if (state != kDeviceStateConnecting || ++self->ptt_release_polls_ > 150) {
            esp_timer_stop(self->ptt_release_timer_);
        }
    }

    void InitializeButtons() {
        esp_timer_create_args_t args = {
            .callback = OnPttReleaseTimer,
            .arg = this,
            .dispatch_method = ESP_TIMER_TASK,
            .name = "ptt_release",
            .skip_unhandled_events = true,
        };
        esp_timer_create(&args, &ptt_release_timer_);

        boot_button_.OnPressDown([this]() { PttDown(); });
        boot_button_.OnPressUp([this]() { PttUp(); });
        boot_button_.OnClick([this]() {
            if (Application::GetInstance().GetDeviceState() == kDeviceStateStarting) {
                EnterWifiConfigMode();
            }
        });
        // PWR click: switch to the best available Wi-Fi now (e.g. hotspot -> home).
        pwr_button_.OnClick([this]() {
            auto state = Application::GetInstance().GetDeviceState();
            if (state == kDeviceStateStarting || state == kDeviceStateWifiConfiguring) {
                return;
            }
            ESP_LOGI(TAG, "PWR click -> switch to the best available Wi-Fi");
            GetDisplay()->ShowNotification("寻找更优网络…", 5000);
            ShuttleWifi::GetInstance().RequestBestNetwork();
        });
        // PWR double click opens the Wi-Fi portal. (Not BOOT x3: talking on a bouncy key
        // produced triple clicks.)
        pwr_button_.OnDoubleClick([this]() {
            ESP_LOGI(TAG, "PWR x2 -> Wi-Fi config portal");
            EnterWifiConfigMode();
        });

        // PWR long press = power off, as on the upstream Waveshare board.
        pwr_button_.OnLongPress([this]() {
            display_->SetTaskState("off", "已关机，长按电源键开机");
            vTaskDelay(pdMS_TO_TICKS(1000));
            power_->PowerAudioOff();
            power_->PowerEpdOff();
            power_->VbatPowerOff();
        });
    }

    void PttDown() {
        ptt_held_ = true;
        auto& app = Application::GetInstance();
        if (app.GetDeviceState() == kDeviceStateStarting) {
            return;  // a click during boot opens the Wi-Fi portal (see OnClick)
        }
        ESP_LOGI(TAG, "PTT pressed (state %d)", (int)app.GetDeviceState());
        app.StartListening();
    }

    void PttUp() {
        ptt_held_ = false;
        auto& app = Application::GetInstance();
        auto state = app.GetDeviceState();
        if (state == kDeviceStateStarting) {
            return;
        }
        ESP_LOGI(TAG, "PTT released (state %d)", (int)state);
        if (state == kDeviceStateListening || state == kDeviceStateConnecting) {
            StartReleaseTail();  // keep recording the tail, then stop (OnPttReleaseTimer)
        } else {
            app.StopListening();
        }
    }

    // ---- debug console (CONFIG_SHUTTLE_DEBUG_CONSOLE, tools/esp_console.py --fifo) ----
    void InitializeDebugConsole() {
#if CONFIG_SHUTTLE_DEBUG_CONSOLE
        esp_timer_create_args_t args = {
            .callback = [](void* arg) { static_cast<ShuttleBoard*>(arg)->PttUp(); },
            .arg = this,
            .dispatch_method = ESP_TIMER_TASK,
            .name = "console_ptt",
            .skip_unhandled_events = true,
        };
        esp_timer_create(&args, &console_ptt_timer_);
        shuttle::StartDebugConsole([this](const std::string& line) { OnConsoleCommand(line); });
#endif
    }

    // Runs in the console task.
    void OnConsoleCommand(const std::string& line) {
        auto& app = Application::GetInstance();
        std::string cmd = line.substr(0, line.find(' '));
        int arg = line.size() > cmd.size() ? atoi(line.c_str() + cmd.size() + 1) : 0;
        if (cmd == "ptt") {  // hold the light-bulb key for <ms>
            esp_timer_stop(console_ptt_timer_);
            PttDown();
            esp_timer_start_once(console_ptt_timer_, (arg > 0 ? arg : 2500) * 1000LL);
        } else if (cmd == "tap" || cmd == "hold") {
            auto gesture = cmd == "tap" ? ShuttleTouch::Gesture::kTap : ShuttleTouch::Gesture::kLongPress;
            app.Schedule([this, gesture]() { OnTouch(gesture); });
        } else if (cmd == "pwr") {
            ShuttleWifi::GetInstance().RequestBestNetwork();
        } else if (cmd == "dump") {
            display_->DumpScreen();
        } else if (cmd == "stats") {  // task CPU usage over <ms>
            SystemInfo::PrintTaskCpuUsage(pdMS_TO_TICKS(arg > 0 ? arg : 1000));
            SystemInfo::PrintHeapStats();
        } else if (cmd == "reboot") {
            esp_restart();
        } else {
            ESP_LOGI(TAG, "commands: ptt <ms> | tap | hold | pwr | dump | stats <ms> | reboot");
        }
    }

    // ---- touch (ESP32-S3-Touch-ePaper-1.54 variant) ---------------------------------
    void InitializeTouch() {
        if (i2c_devices_.find("0x38") == std::string::npos) {
            ESP_LOGI(TAG, "No touch panel on this board");
            return;
        }
        touch_.Start(i2c_bus_, SHUTTLE_TOUCH_INT_GPIO, SHUTTLE_TOUCH_RST_GPIO,
                     [this](ShuttleTouch::Gesture gesture, int x, int y) {
                         Application::GetInstance().Schedule([this, gesture]() { OnTouch(gesture); });
                     });
    }

    // Tap while busy = cancel the task; tap while idle = device details; long press while
    // idle = ghost-clearing full refresh.
    void OnTouch(ShuttleTouch::Gesture gesture) {
        if (display_ == nullptr) {
            return;
        }
        auto& app = Application::GetInstance();
        auto state = app.GetDeviceState();
        bool busy = display_->TaskBusy() || state == kDeviceStateSpeaking;
        if (gesture == ShuttleTouch::Gesture::kTap && busy) {
            ESP_LOGI(TAG, "Touch: cancel task (state %d)", (int)state);
            app.AbortSpeaking(kAbortReasonNone);  // -> {"type":"abort"}: server cancels the task
            display_->SetTaskState("cancelled", "已取消");
            return;
        }
        if (state != kDeviceStateIdle) {
            return;
        }
        if (gesture == ShuttleTouch::Gesture::kTap) {
            display_->ShowDetails(DetailsText());
        } else {
            display_->RequestFullRefresh();
        }
    }

    std::string DetailsText() {
        std::string text;
        wifi_ap_record_t ap = {};
        if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
            text += std::string((const char*)ap.ssid) + " " + std::to_string(ap.rssi) + "dBm\n";
        } else {
            text += "Wi-Fi 未连接\n";
        }
        text += "IP " + shuttle::GetStaIp() + "\n";
        std::string server = shuttle::GetServerBaseUrl();
        auto pos = server.find("://");
        text += "→" + (pos == std::string::npos ? server : server.substr(pos + 3)) + "\n";
        std::string phone = server_phone_link_ == "mac"     ? "Mac"
                            : server_phone_link_ == "esp32" ? (phone_name_.empty() ? "本机桥" : phone_name_)
                                                            : "?";
        std::string adb = server_adb_state_ == "device"         ? "已连接"
                          : server_adb_state_ == "unauthorized" ? "未授权"
                          : server_adb_state_.empty()           ? "未知"
                                                                : server_adb_state_;
        text += "手机:" + phone + " " + adb;
        int level = 0;
        bool charging = false, discharging = false;
        GetBatteryLevel(level, charging, discharging);
        text = "v" + std::string(esp_app_get_description()->version) + " 电量" + std::to_string(level) + "%\n" + text;
        return text;
    }

    void InitializeTools() {
        auto& mcp_server = McpServer::GetInstance();
        mcp_server.AddTool("self.disp.network", "重新配网", PropertyList(), [this](const PropertyList&) -> ReturnValue {
            EnterWifiConfigMode();
            return true;
        });
    }

    void InitializeDisplay() {
        custom_lcd_spi_t spi = {};
        spi.cs = EPD_CS_PIN;
        spi.dc = EPD_DC_PIN;
        spi.rst = EPD_RST_PIN;
        spi.busy = EPD_BUSY_PIN;
        spi.mosi = EPD_MOSI_PIN;
        spi.scl = EPD_SCK_PIN;
        spi.spi_host = EPD_SPI_NUM;
        spi.buffer_len = EXAMPLE_LCD_WIDTH * EXAMPLE_LCD_HEIGHT / 8;
        display_ = new ShuttleDisplay(EXAMPLE_LCD_WIDTH, EXAMPLE_LCD_HEIGHT, spi);
    }

    void InitializePower() {
        power_ = new BoardPowerBsp(EPD_PWR_PIN, Audio_PWR_PIN, VBAT_PWR_PIN);
        power_->VbatPowerOn();
        power_->PowerAudioOn();
        power_->PowerEpdOn();
        do {
            vTaskDelay(pdMS_TO_TICKS(10));
        } while (!gpio_get_level(VBAT_PWR_GPIO));
    }

    // ---- battery (same ADC math as the upstream Waveshare board) ------------------
    uint16_t BatteryGetVoltage() {
        static bool initialized = false;
        static adc_oneshot_unit_handle_t adc_handle;
        static adc_cali_handle_t cali_handle = nullptr;
        if (!initialized) {
            adc_oneshot_unit_init_cfg_t init_config = {
                .unit_id = ADC_UNIT_1,
            };
            adc_oneshot_new_unit(&init_config, &adc_handle);
            adc_oneshot_chan_cfg_t ch_config = {
                .atten = ADC_ATTEN_DB_12,
                .bitwidth = ADC_BITWIDTH_12,
            };
            adc_oneshot_config_channel(adc_handle, ADC_CHANNEL_3, &ch_config);
            adc_cali_curve_fitting_config_t cali_config = {
                .unit_id = ADC_UNIT_1,
                .atten = ADC_ATTEN_DB_12,
                .bitwidth = ADC_BITWIDTH_12,
            };
            if (adc_cali_create_scheme_curve_fitting(&cali_config, &cali_handle) == ESP_OK) {
                initialized = true;
            }
        }
        if (initialized) {
            int raw_value = 0;
            int raw_voltage = 0;
            adc_oneshot_read(adc_handle, ADC_CHANNEL_3, &raw_value);
            adc_cali_raw_to_voltage(cali_handle, raw_value, &raw_voltage);
            return (uint16_t)(raw_voltage * 2);  // 1:2 divider
        }
        return 0;
    }

    uint8_t BatteryGetPercent() {
        int voltage = 0;
        for (uint8_t i = 0; i < 10; i++) {
            voltage += BatteryGetVoltage();
        }
        voltage /= 10;
        int percent = (-1 * voltage * voltage + 9016 * voltage - 19189000) / 10000;
        percent = (percent > 100) ? 100 : (percent < 0) ? 0 : percent;
        return (uint8_t)percent;
    }

    // ---- ADB bridge ---------------------------------------------------------------
    void InitializeBridge() {
#if CONFIG_SHUTTLE_ADB_BRIDGE_ENABLE
        auto cfg = shuttle::AdbBridge::DefaultConfig();
        cfg.device_id = SystemInfo::GetMacAddress();
        bridge_ = std::make_unique<shuttle::AdbBridge>(cfg, []() {
            shuttle::AdbBridge::Target t;
            t.ssid = WifiManager::GetInstance().GetSsid();
            switch (shuttle::GetPhoneHostMode(t.ssid)) {
                case shuttle::PhoneHostMode::kAuto: t.mode = shuttle::AdbBridge::Target::Mode::kAuto; break;
                case shuttle::PhoneHostMode::kFixed:
                    t.mode = shuttle::AdbBridge::Target::Mode::kFixed;
                    t.fixed_host = shuttle::GetFixedPhoneHost(t.ssid);
                    break;
                case shuttle::PhoneHostMode::kDisabled: t.mode = shuttle::AdbBridge::Target::Mode::kDisabled; break;
            }
            return t;
        });
        bridge_->SetStatusCallback([this](const shuttle::AdbBridgeStatus&) {
            Application::GetInstance().Schedule([this]() { UpdatePhoneLine(); });
        });
        bridge_->Start();
        esp_timer_create_args_t args = {
            .callback = OnPowerSaveTimer,
            .arg = this,
            .dispatch_method = ESP_TIMER_TASK,
            .name = "adb_power_save",
            .skip_unhandled_events = true,
        };
        esp_timer_create(&args, &ps_timer_);
        esp_timer_start_periodic(ps_timer_, 250 * 1000);
#endif
    }

    // ---- Wi-Fi power save: off while ADB data flows ------------------------------------
    // xiaozhi turns power saving off only while the voice channel is open. In its idle level
    // (MAX_MODEM) the radio wakes up every few beacons: the bridge then moved ~2 KB/s and its
    // WebSocket timed out in the middle of a screenshot. Off while bridge traffic flows and for
    // kAdbIdleUs after it, then back to whatever xiaozhi asked for.
    static constexpr int64_t kAdbIdleUs = 10LL * 1000 * 1000;

    void SetPowerSaveLevel(PowerSaveLevel level) override {
        {
            std::lock_guard<std::mutex> lock(ps_mutex_);
            requested_ps_ = level;
        }
        ApplyPowerSave();
    }

    void ApplyPowerSave() {
        std::lock_guard<std::mutex> lock(ps_mutex_);
        PowerSaveLevel level = adb_busy_.load() ? PowerSaveLevel::PERFORMANCE : requested_ps_;
        if (ps_applied_ && level == applied_ps_) {
            return;
        }
        ps_applied_ = true;
        applied_ps_ = level;
        WifiBoard::SetPowerSaveLevel(level);
    }

    static void OnPowerSaveTimer(void* arg) {
        auto* self = static_cast<ShuttleBoard*>(arg);
        auto st = self->bridge_->GetStatus();
        uint64_t bytes = st.bytes_to_phone + st.bytes_from_phone;
        int64_t now = esp_timer_get_time();
        if (bytes != self->ps_bytes_) {
            self->ps_bytes_ = bytes;
            self->ps_traffic_us_ = now;
        }
        bool busy = self->ps_traffic_us_ != 0 && now - self->ps_traffic_us_ < kAdbIdleUs;
        if (busy != self->adb_busy_.load()) {
            self->adb_busy_ = busy;
            ESP_LOGI(TAG, "ADB traffic %s: Wi-Fi power save %s", busy ? "flowing" : "idle",
                     busy ? "off" : "back to normal");
            self->ApplyPowerSave();
        }
    }

    // Phone/server icons and the idle hint. The phone is reached either through this
    // device's ADB bridge (phone hotspot) or directly by the Mac (USB / LAN: the server
    // reports link "mac"); the server's "phone" event tells which.
    void UpdatePhoneLine() {
        if (display_ == nullptr) {
            return;
        }
        int server = server_seen_ ? 1 : -1;
        bool phone_ok = false;
        bool home = false;
        std::string problem;  // shown while idle: what is wrong and what to do
        auto adb_problem = [this]() -> std::string {
            if (server_adb_state_ == "unauthorized") return "请在手机上允许 USB 调试";
            if (server_adb_state_ == "offline") return "手机离线，请重新插拔或重连";
            return "";
        };
        if (server_phone_link_ == "mac") {
            // The Mac reaches the phone itself (USB / LAN): the bridge does not matter.
            phone_ok = server_adb_state_ == "device";
            problem = phone_ok ? "" : adb_problem();
            if (!phone_ok && problem.empty()) problem = "Mac 上没有连接手机";
        } else if (bridge_) {
            auto st = bridge_->GetStatus();
            switch (st.state) {
                case shuttle::LinkState::kDisabled:  // no ADB on this network (PHONE_HOST=none)
                case shuttle::LinkState::kWaitingNetwork:
                case shuttle::LinkState::kConnectingServer:
                    break;
                case shuttle::LinkState::kScanning:
                    problem = "正在找手机…";
                    break;
                case shuttle::LinkState::kNoPhone:
                    problem = "没找到手机：手机连同一 Wi-Fi，执行 adb tcpip 5555";
                    break;
                case shuttle::LinkState::kServerUnreachable:
                    server = 0;
                    problem = "连不上服务器：检查网络或服务器地址";
                    break;
                case shuttle::LinkState::kConnectingPhone:
                    server = 1;
                    problem = "正在连接手机…";
                    break;
                case shuttle::LinkState::kPhoneUnreachable:
                    server = 1;  // it went away (a reboot drops `adb tcpip`); a rescan follows
                    problem = "连不上手机：手机重启后要重新执行 adb tcpip 5555";
                    break;
                case shuttle::LinkState::kActive:
                    server = 1;
                    problem = adb_problem();
                    phone_ok = problem.empty();
                    break;
            }
            home = st.state == shuttle::LinkState::kActive && !st.phone_host.empty() && st.phone_host != st.gateway;
            phone_name_ = st.phone_name;
            if (st.phones_found > 1) {
                phone_name_ += " +" + std::to_string(st.phones_found - 1);  // more phones around
            }
        }
        if (!phone_ok && problem.empty() && server_phone_link_ == "none") {
            problem = "手机未连接";
        }
        if (home != home_network_) {
            home_network_ = home;
            display_->SetHomeNetwork(home);
        }
        display_->SetPhoneLink(phone_ok, problem);
        display_->SetServerLink(server);
    }

    bool IsIdleForRoaming() {
        if (Application::GetInstance().GetDeviceState() != kDeviceStateIdle) {
            return false;
        }
        if (bridge_) {
            auto st = bridge_->GetStatus();
            uint64_t bytes = st.bytes_from_phone + st.bytes_to_phone;
            int64_t now = esp_timer_get_time();
            if (bytes != last_bridge_bytes_) {
                last_bridge_bytes_ = bytes;
                last_bridge_activity_us_ = now;
            }
            if (now - last_bridge_activity_us_ < 60LL * 1000 * 1000) {
                return false;  // ADB in use (agent may be working)
            }
        }
        return true;
    }

public:
    ShuttleBoard() : boot_button_(BOOT_BUTTON_GPIO), pwr_button_(VBAT_PWR_GPIO) {
        ESP_LOGI(TAG, "Shuttle board init");
        shuttle::ProvisionXiaozhiSettings();  // ws url/token/ota url from Kconfig -> NVS
        InitializePower();
        InitializeI2c();
        InitializeTouch();
        InitializeButtons();
        InitializeTools();
        InitializeDisplay();
        InitializeBridge();
        InitializeDebugConsole();
    }

    void StartNetwork() override {
        // Derived from WifiBoard::StartNetwork(); differences: faster rescans, Shuttle
        // priority logic, no automatic config portal when known networks are absent.
        auto& wifi_manager = WifiManager::GetInstance();
        WifiManagerConfig config;
        config.ssid_prefix = "Shuttle";
        config.language = Lang::CODE;
        config.show_ota_config = false;  // server address comes from Kconfig
        config.show_sleep_config = true;
        config.station_scan_min_interval_seconds = 5;
        config.station_scan_max_interval_seconds = 30;
        uint8_t mac[6];
        if (esp_read_mac(mac, ESP_MAC_WIFI_STA) == ESP_OK) {
            char hostname[32];
            snprintf(hostname, sizeof(hostname), "Shuttle-%02X%02X", mac[4], mac[5]);
            config.station_hostname = hostname;
        }
        wifi_manager.Initialize(config);

        wifi_manager.SetEventCallback([this](WifiEvent event, const std::string& data) {
            ShuttleWifi::GetInstance().OnWifiEvent(event, data);
            switch (event) {
                case WifiEvent::Scanning:
                    OnNetworkEvent(NetworkEvent::Scanning);
                    break;
                case WifiEvent::Connecting:
                    OnNetworkEvent(NetworkEvent::Connecting, data);
                    break;
                case WifiEvent::Connected: {
                    // Per-network server (e.g. the Mac's LAN address at home), before xiaozhi's
                    // version check and the bridge connect.
                    shuttle::SetCurrentNetwork(data);
                    shuttle::ProvisionXiaozhiSettings();
                    if (bridge_) bridge_->SetServerUrl(shuttle::GetAdbUrl());
                    auto mode = shuttle::GetPhoneHostMode(data);
                    ESP_LOGI(TAG, "Wi-Fi '%s' ip=%s gw=%s, phone: %s %s", data.c_str(), shuttle::GetStaIp().c_str(),
                             shuttle::GetStaGatewayIp().c_str(), shuttle::PhoneHostModeName(mode),
                             shuttle::GetFixedPhoneHost(data).c_str());
                    // The USB console usually reconnects after the board init: repeat it here.
                    ESP_LOGI(TAG, "I2C devices: %s", i2c_devices_.c_str());
                    Application::GetInstance().Schedule([this]() { UpdatePhoneLine(); });
                    OnNetworkEvent(NetworkEvent::Connected, data);
                    if (bridge_) bridge_->SetNetworkUp(true);
                    break;
                }
                case WifiEvent::Disconnected:
                    if (bridge_) bridge_->SetNetworkUp(false);
                    Application::GetInstance().Schedule([this]() {
                        home_network_ = false;
                        display_->SetHomeNetwork(false);
                    });
                    OnNetworkEvent(NetworkEvent::Disconnected);
                    break;
                case WifiEvent::ConfigModeEnter:
                    if (bridge_) bridge_->SetNetworkUp(false);
                    OnNetworkEvent(NetworkEvent::WifiConfigModeEnter);
                    break;
                case WifiEvent::ConfigModeExit:
                    OnNetworkEvent(NetworkEvent::WifiConfigModeExit);
                    break;
            }
        });

        auto& shuttle_wifi = ShuttleWifi::GetInstance();
        shuttle_wifi.Init();
        if (!shuttle_wifi.HasNetworks()) {
            ESP_LOGW(TAG, "No Wi-Fi configured (Kconfig or portal) -> config portal");
            vTaskDelay(pdMS_TO_TICKS(1500));
            StartWifiConfigMode();
            return;
        }
        shuttle_wifi.Start([this]() { return IsIdleForRoaming(); });
    }

    // Called from the "shuttle" JSON hook (protocol task).
    void HandleShuttleMessage(const cJSON* root) {
        auto event = cJSON_GetObjectItem(root, "event");
        if (!cJSON_IsString(event)) {
            return;
        }
        auto& app = Application::GetInstance();
        if (!server_seen_) {
            app.Schedule([this]() {
                server_seen_ = true;
                UpdatePhoneLine();
            });
        }
        if (strcmp(event->valuestring, "status") == 0) {
            auto state = cJSON_GetObjectItem(root, "state");
            auto text = cJSON_GetObjectItem(root, "text");
            std::string s = cJSON_IsString(state) ? state->valuestring : "";
            std::string t = cJSON_IsString(text) ? text->valuestring : "";
            ESP_LOGI(TAG, "Task status: %s %s", s.c_str(), t.c_str());
            app.Schedule([this, s, t]() { display_->SetTaskState(s, t); });
        } else if (strcmp(event->valuestring, "phone") == 0) {
            auto adb = cJSON_GetObjectItem(root, "adb");
            auto link = cJSON_GetObjectItem(root, "link");
            std::string a = cJSON_IsString(adb) ? adb->valuestring : "";
            std::string l = cJSON_IsString(link) ? link->valuestring : "";
            ESP_LOGI(TAG, "Server reports phone: adb=%s link=%s", a.c_str(), l.c_str());
            app.Schedule([this, a, l]() {
                server_adb_state_ = a;
                server_phone_link_ = l;
                UpdatePhoneLine();
            });
        }
        // "ping": keep-alive only (resets the xiaozhi channel timeout), nothing to do.
    }

    AudioCodec* GetAudioCodec() override {
        static ShuttleAudioCodec audio_codec(i2c_bus_, I2C_NUM_0, AUDIO_INPUT_SAMPLE_RATE, AUDIO_OUTPUT_SAMPLE_RATE,
                                             AUDIO_I2S_GPIO_MCLK, AUDIO_I2S_GPIO_BCLK, AUDIO_I2S_GPIO_WS,
                                             AUDIO_I2S_GPIO_DOUT, AUDIO_I2S_GPIO_DIN, AUDIO_CODEC_PA_PIN,
                                             AUDIO_CODEC_ES8311_ADDR);
        return &audio_codec;
    }

    Display* GetDisplay() override { return display_; }

    bool GetBatteryLevel(int& level, bool& charging, bool& discharging) override {
        // TODO(hw): the board has no documented charge-status GPIO; report discharging.
        charging = false;
        discharging = true;
        level = (int)BatteryGetPercent();
        return true;
    }
};

DECLARE_BOARD(ShuttleBoard);

// Hook declared in main/application.cc (CONFIG_SHUTTLE_ENABLE). Runs in the protocol task.
bool ShuttleHandleIncomingJson(const cJSON* root) {
    auto type = cJSON_GetObjectItem(root, "type");
    if (!cJSON_IsString(type) || strcmp(type->valuestring, "shuttle") != 0) {
        return false;
    }
    static_cast<ShuttleBoard&>(Board::GetInstance()).HandleShuttleMessage(root);
    return true;
}
