// Shuttle board: Waveshare ESP32-S3-ePaper-1.54 V2 + push-to-talk + ADB bridge.
//
// Derived from main/boards/waveshare/esp32-s3-epaper-1.54/waveshare-s3-epaper-1.54.cc.
// Differences from upstream (everything else, incl. the PWR key power behaviour, is kept):
//   * BOOT key is push-to-talk (hold to record, release to send) instead of toggle.
//   * Custom e-paper UI (ShuttleDisplay) with phone/ADB link and battery percentage.
//   * Wi-Fi uses ShuttleWifi (priority order + roam-up) and never falls into the config
//     portal just because the hotspot is off; triple-click BOOT opens the portal.
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
#include <esp_timer.h>

#include <cJSON.h>

#include <atomic>
#include <cstring>
#include <memory>

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
#include "shuttle/shuttle_config.h"
#include "shuttle_display.h"
#include "shuttle_wifi.h"

#define TAG "ShuttleBoard"

class ShuttleBoard : public WifiBoard {
private:
    i2c_master_bus_handle_t i2c_bus_ = nullptr;
    Button boot_button_;
    Button pwr_button_;
    ShuttleDisplay* display_ = nullptr;
    BoardPowerBsp* power_ = nullptr;
    std::unique_ptr<shuttle::AdbBridge> bridge_;

    std::atomic<bool> ptt_held_{false};
    std::atomic<bool> ptt_active_{false};
    esp_timer_handle_t ptt_hold_timer_ = nullptr;
    esp_timer_handle_t ptt_release_timer_ = nullptr;
    int ptt_release_polls_ = 0;

    std::string server_adb_state_;  // from server: device / unauthorized / offline / ...
    bool home_network_ = false;     // the phone in use is not the hotspot (reached via a router)
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
    }

    // ---- BOOT key: push-to-talk + click gestures ---------------------------------------
    // PTT only starts after the key has been held for kPttHoldMs, so quick clicks are free
    // for gestures (x2 = switch to the best Wi-Fi now, x3 = Wi-Fi portal) and accidental taps
    // do not open the voice channel.
    static constexpr int kPttHoldMs = 250;

    static void OnPttHoldTimer(void* arg) {
        auto* self = static_cast<ShuttleBoard*>(arg);
        if (!self->ptt_held_) {
            return;
        }
        self->ptt_active_ = true;
        auto& app = Application::GetInstance();
        ESP_LOGI(TAG, "PTT start (state %d)", (int)app.GetDeviceState());
        app.StartListening();
        // Released while we were starting: behave like a normal release.
        if (!self->ptt_held_ && self->ptt_active_.exchange(false)) {
            app.StopListening();
            self->ptt_release_polls_ = 0;
            esp_timer_stop(self->ptt_release_timer_);
            esp_timer_start_periodic(self->ptt_release_timer_, 200 * 1000);
        }
    }

    static void OnPttReleaseTimer(void* arg) {
        // The release may happen while the audio channel is still connecting
        // (Connecting state ignores StopListening). Keep stopping until we leave it.
        auto* self = static_cast<ShuttleBoard*>(arg);
        auto& app = Application::GetInstance();
        auto state = app.GetDeviceState();
        if (self->ptt_held_) {
            return;
        }
        if (state == kDeviceStateListening) {
            ESP_LOGI(TAG, "PTT released during connect -> stop listening");
            app.StopListening();
            esp_timer_stop(self->ptt_release_timer_);
        } else if (state != kDeviceStateConnecting || ++self->ptt_release_polls_ > 75) {
            esp_timer_stop(self->ptt_release_timer_);
        }
    }

    void InitializeButtons() {
        esp_timer_create_args_t hold_args = {
            .callback = OnPttHoldTimer,
            .arg = this,
            .dispatch_method = ESP_TIMER_TASK,
            .name = "ptt_hold",
            .skip_unhandled_events = true,
        };
        esp_timer_create(&hold_args, &ptt_hold_timer_);
        esp_timer_create_args_t args = {
            .callback = OnPttReleaseTimer,
            .arg = this,
            .dispatch_method = ESP_TIMER_TASK,
            .name = "ptt_release",
            .skip_unhandled_events = true,
        };
        esp_timer_create(&args, &ptt_release_timer_);

        boot_button_.OnPressDown([this]() {
            ptt_held_ = true;
            if (Application::GetInstance().GetDeviceState() == kDeviceStateStarting) {
                return;  // a click during boot opens the Wi-Fi portal (see OnClick)
            }
            esp_timer_stop(ptt_hold_timer_);
            esp_timer_start_once(ptt_hold_timer_, kPttHoldMs * 1000);
        });
        boot_button_.OnPressUp([this]() {
            ptt_held_ = false;
            esp_timer_stop(ptt_hold_timer_);
            if (!ptt_active_.exchange(false)) {
                return;  // short click: handled by the click callbacks below
            }
            auto& app = Application::GetInstance();
            ESP_LOGI(TAG, "PTT release (state %d)", (int)app.GetDeviceState());
            app.StopListening();
            ptt_release_polls_ = 0;
            esp_timer_stop(ptt_release_timer_);
            esp_timer_start_periodic(ptt_release_timer_, 200 * 1000);
        });
        boot_button_.OnClick([this]() {
            if (Application::GetInstance().GetDeviceState() == kDeviceStateStarting) {
                EnterWifiConfigMode();
            }
        });
        boot_button_.OnDoubleClick([this]() {
            auto state = Application::GetInstance().GetDeviceState();
            if (state == kDeviceStateStarting || state == kDeviceStateWifiConfiguring) {
                return;
            }
            ESP_LOGI(TAG, "BOOT x2 -> switch to the best available Wi-Fi");
            GetDisplay()->ShowNotification("寻找更优网络…", 5000);
            ShuttleWifi::GetInstance().RequestBestNetwork();
        });
        boot_button_.OnMultipleClick(
            [this]() {
                ESP_LOGI(TAG, "BOOT x3 -> Wi-Fi config portal");
                EnterWifiConfigMode();
            },
            3);

        // PWR key: identical to the upstream Waveshare board (long press = power off).
        pwr_button_.OnLongPress([this]() {
            GetDisplay()->SetChatMessage("system", "OFF");
            vTaskDelay(pdMS_TO_TICKS(1000));
            power_->PowerAudioOff();
            power_->PowerEpdOff();
            power_->VbatPowerOff();
        });
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
#endif
    }

    void UpdatePhoneLine() {
        if (display_ == nullptr) {
            return;
        }
        if (!bridge_) {
            display_->SetPhoneLink(false, "ADB桥关闭");
            return;
        }
        auto st = bridge_->GetStatus();
        bool server_ok = false;
        bool phone_ok = false;
        std::string text;
        switch (st.state) {
            case shuttle::LinkState::kDisabled: text = "未配置手机"; break;
            case shuttle::LinkState::kWaitingNetwork: text = "等待Wi-Fi"; break;
            case shuttle::LinkState::kScanning: text = "搜索手机"; break;
            case shuttle::LinkState::kNoPhone: text = "未发现手机"; break;
            case shuttle::LinkState::kConnectingServer: text = "连接服务器"; break;
            case shuttle::LinkState::kServerUnreachable: text = "服务器断开"; break;
            case shuttle::LinkState::kConnectingPhone:
                server_ok = true;
                text = "连接手机";
                break;
            case shuttle::LinkState::kPhoneUnreachable:
                server_ok = true;
                text = "连不上手机";  // it went away (reboot drops `adb tcpip`); a rescan follows
                break;
            case shuttle::LinkState::kActive:
                server_ok = true;
                phone_ok = true;
                if (server_adb_state_ == "unauthorized") {
                    text = "请手机授权";
                } else if (server_adb_state_ == "offline") {
                    text = "手机离线";
                    phone_ok = false;
                } else if (!st.phone_name.empty()) {
                    text = st.phone_name;
                    if (st.phones_found > 1) {
                        text += " +" + std::to_string(st.phones_found - 1);  // more phones around
                    }
                } else {
                    text = "手机已连接";
                }
                break;
        }
        bool home = st.state == shuttle::LinkState::kActive && !st.phone_host.empty() && st.phone_host != st.gateway;
        if (home != home_network_) {
            home_network_ = home;
            display_->SetHomeNetwork(home);
        }
        display_->SetPhoneLink(phone_ok, text);
        display_->SetServerLink(server_ok);
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
        InitializeButtons();
        InitializeTools();
        InitializeDisplay();
        InitializeBridge();
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
                    auto mode = shuttle::GetPhoneHostMode(data);
                    ESP_LOGI(TAG, "Wi-Fi '%s' ip=%s gw=%s, phone: %s %s", data.c_str(), shuttle::GetStaIp().c_str(),
                             shuttle::GetStaGatewayIp().c_str(), shuttle::PhoneHostModeName(mode),
                             shuttle::GetFixedPhoneHost(data).c_str());
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
        if (strcmp(event->valuestring, "status") == 0) {
            auto state = cJSON_GetObjectItem(root, "state");
            auto text = cJSON_GetObjectItem(root, "text");
            std::string s = cJSON_IsString(state) ? state->valuestring : "";
            std::string t = cJSON_IsString(text) ? text->valuestring : "";
            ESP_LOGI(TAG, "Task status: %s %s", s.c_str(), t.c_str());
            app.Schedule([this, s, t]() { display_->SetTaskState(s, t); });
        } else if (strcmp(event->valuestring, "phone") == 0) {
            auto adb = cJSON_GetObjectItem(root, "adb");
            std::string a = cJSON_IsString(adb) ? adb->valuestring : "";
            ESP_LOGI(TAG, "Server reports phone adb state: %s", a.c_str());
            app.Schedule([this, a]() {
                server_adb_state_ = a;
                UpdatePhoneLine();
            });
        }
        // "ping": keep-alive only (resets the xiaozhi channel timeout), nothing to do.
    }

    AudioCodec* GetAudioCodec() override {
        static Es8311AudioCodec audio_codec(i2c_bus_, I2C_NUM_0, AUDIO_INPUT_SAMPLE_RATE, AUDIO_OUTPUT_SAMPLE_RATE,
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
