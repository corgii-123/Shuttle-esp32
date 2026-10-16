// ADB pass-through: one TCP connection to the phone's adbd (<phone>:5555) is spliced
// byte-for-byte with one WebSocket connection to <server>/ws/adb. The ESP32 never
// parses ADB; the Mac's native adb talks to the phone through this "long cable".
//
// Lifecycle (see docs/protocol.md):
//   network up -> scan for phones (PhoneScanner) -> connect WS -> hello{candidates}
//   -> server answers connect{host} (or we pick: last / gateway / first after 3 s)
//   -> connect phone TCP -> pump bytes both ways
//   either side closes -> close the other -> back off -> reconnect (new WS session)
//   server close code 4005 = "switch phone": reconnect immediately, ask again.
// A background scan (CONFIG_SHUTTLE_PHONE_SCAN_INTERVAL_S) reports new phones on the open
// session ({"type":"candidates"}), so the server can move to a phone that just ran
// `adb tcpip 5555`.
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
#include <vector>

#include "shuttle/backoff.h"
#include "shuttle/phone_scanner.h"

struct esp_websocket_client;

namespace shuttle {

enum class LinkState {
    kDisabled,           // bridge disabled for the current network
    kWaitingNetwork,     // no Wi-Fi / no IP
    kScanning,           // looking for phones (first scan on this network)
    kNoPhone,            // scanned, no phone with `adb tcpip` found
    kConnectingServer,   // opening wss://.../ws/adb
    kConnectingPhone,    // WS up, connecting TCP to the phone
    kPhoneUnreachable,   // WS up but phone adbd refused/timeout (adb tcpip not run?)
    kServerUnreachable,  // WS connect failed
    kActive,             // both legs up, bytes flowing
};

const char* LinkStateName(LinkState state);

struct AdbBridgeStatus {
    LinkState state = LinkState::kWaitingNetwork;
    std::string phone_host;  // host of the current / last session
    std::string phone_name;  // from the server ({"type":"phone_info"}), "" until identified
    std::string gateway;     // DHCP gateway (phone_host == gateway: the hotspot phone)
    int phones_found = 0;    // candidates from the last scan
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

    // How to find the phone on the current network.
    struct Target {
        enum class Mode { kAuto, kFixed, kDisabled } mode = Mode::kDisabled;
        std::string fixed_host;  // kFixed
        std::string ssid;
    };
    using TargetResolver = std::function<Target()>;
    // Called from the bridge task whenever the status changes. Keep it short.
    using StatusCallback = std::function<void(const AdbBridgeStatus&)>;

    static Config DefaultConfig();  // from Kconfig + shuttle_config

    AdbBridge(Config config, TargetResolver resolver);
    ~AdbBridge();
    AdbBridge(const AdbBridge&) = delete;
    AdbBridge& operator=(const AdbBridge&) = delete;

    bool Start();
    void SetNetworkUp(bool up);  // thread-safe
    void RequestRescan();        // thread-safe: scan for phones now
    void SetStatusCallback(StatusCallback callback);
    AdbBridgeStatus GetStatus();

private:
    static void BridgeTaskEntry(void* arg);
    static void WriterTaskEntry(void* arg);
    static void ScanTaskEntry(void* arg);
    static void WsEventHandler(void* arg, const char* base, int32_t event_id, void* event_data);

    void BridgeTask();
    void WriterTask();
    void ScanTask();
    bool OpenWebSocket();
    std::string NegotiateHost(const Target& target, const std::vector<std::string>& candidates);
    void SendCandidates(const std::vector<std::string>& candidates);
    void HandleServerText(const std::string& msg);
    std::vector<std::string> GetCandidates();
    void DropCandidate(const std::string& host);
    void CloseWebSocket(int code, const char* reason);
    int ConnectPhone(const std::string& host);
    void RunSession(int sock);
    void EndSession();
    void SendControl(const char* state, const std::string& host, const std::string& detail);
    void SendText(const std::string& msg);
    void SetState(LinkState state);
    void DelayInterruptible(int ms);

    Config config_;
    TargetResolver resolver_;
    PhoneScanner scanner_;
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
    TaskHandle_t scan_task_ = nullptr;

    std::mutex cand_mutex_;  // guards candidates_, scanned_, server_host_, current_host_
    std::vector<std::string> candidates_;
    bool scanned_ = false;      // a scan finished on this network
    std::string server_host_;   // from {"type":"connect"}; "" = {"type":"idle"}
    std::string current_host_;  // spliced right now
    std::atomic<bool> scan_abort_{false};
    std::atomic<bool> switch_requested_{false};
    std::atomic<int> server_close_code_{0};

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
