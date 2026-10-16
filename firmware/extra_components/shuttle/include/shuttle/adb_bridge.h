// ADB pass-through: one TCP connection to the phone's adbd (<gateway>:5555) is spliced
// byte-for-byte with one WebSocket connection to <server>/ws/adb. The ESP32 never
// parses ADB; the Mac's native adb talks to the phone through this "long cable".
//
// Lifecycle (see docs/protocol.md):
//   network up -> connect WS -> connect phone TCP -> pump bytes both ways
//   either side closes -> close the other -> back off -> reconnect (new WS session)
#pragma once

#include <freertos/FreeRTOS.h>
#include <freertos/event_groups.h>
#include <freertos/semphr.h>
#include <freertos/stream_buffer.h>
#include <freertos/task.h>

#include <atomic>
#include <cstdint>
#include <functional>
#include <mutex>
#include <string>

#include "shuttle/backoff.h"

struct esp_websocket_client;

namespace shuttle {

enum class LinkState {
    kDisabled,           // bridge disabled for the current network
    kWaitingNetwork,     // no Wi-Fi / no IP
    kConnectingServer,   // opening wss://.../ws/adb
    kConnectingPhone,    // WS up, connecting TCP to the phone
    kPhoneUnreachable,   // WS up but phone adbd refused/timeout (adb tcpip not run?)
    kServerUnreachable,  // WS connect failed
    kActive,             // both legs up, bytes flowing
};

const char* LinkStateName(LinkState state);

struct AdbBridgeStatus {
    LinkState state = LinkState::kWaitingNetwork;
    std::string phone_host;
    uint64_t bytes_to_phone = 0;
    uint64_t bytes_from_phone = 0;
    uint32_t sessions = 0;
};

class AdbBridge {
public:
    struct Config {
        std::string url;        // ws(s)://.../ws/adb
        std::string token;      // Bearer token
        std::string device_id;  // MAC, sent as Device-Id header
        int phone_port = 5555;
        size_t rx_buffer_bytes = 256 * 1024;  // WS -> phone, PSRAM stream buffer
        size_t tx_chunk_bytes = 16 * 1024;    // phone -> WS read chunk, PSRAM
        int ws_buffer_size = 8192;
        int backoff_min_ms = 1000;
        int backoff_max_ms = 30000;
        int ping_interval_s = 10;
        int pong_timeout_s = 30;
    };

    // Returns the phone host to dial right now, or "" if the bridge must stay idle.
    using HostResolver = std::function<std::string()>;
    // Called from the bridge task whenever the status changes. Keep it short.
    using StatusCallback = std::function<void(const AdbBridgeStatus&)>;

    static Config DefaultConfig();  // from Kconfig + shuttle_config

    AdbBridge(Config config, HostResolver resolver);
    ~AdbBridge();
    AdbBridge(const AdbBridge&) = delete;
    AdbBridge& operator=(const AdbBridge&) = delete;

    bool Start();
    void SetNetworkUp(bool up);  // thread-safe
    void SetStatusCallback(StatusCallback callback);
    AdbBridgeStatus GetStatus();

private:
    static void BridgeTaskEntry(void* arg);
    static void WriterTaskEntry(void* arg);
    static void WsEventHandler(void* arg, const char* base, int32_t event_id, void* event_data);

    void BridgeTask();
    void WriterTask();
    bool OpenWebSocket();
    void CloseWebSocket(int code, const char* reason);
    int ConnectPhone(const std::string& host);
    void RunSession(int sock);
    void EndSession();
    void SendControl(const char* state, const std::string& detail = "");
    void SetState(LinkState state);
    void DelayInterruptible(int ms);

    Config config_;
    HostResolver resolver_;
    StatusCallback status_callback_;
    std::mutex status_mutex_;
    AdbBridgeStatus status_;

    std::string headers_;
    EventGroupHandle_t events_ = nullptr;
    StreamBufferHandle_t rx_stream_ = nullptr;  // WS -> phone
    uint8_t* tx_chunk_ = nullptr;               // phone -> WS
    uint8_t* writer_buf_ = nullptr;
    SemaphoreHandle_t writer_mutex_ = nullptr;
    TaskHandle_t bridge_task_ = nullptr;
    TaskHandle_t writer_task_ = nullptr;

    esp_websocket_client* ws_ = nullptr;
    std::atomic<bool> accept_rx_{false};
    std::atomic<bool> last_frame_binary_{false};
    std::atomic<int> session_sock_{-1};
    std::atomic<bool> writer_error_{false};
    std::atomic<bool> network_up_{false};

    Backoff server_backoff_;
    Backoff phone_backoff_;
};

}  // namespace shuttle
