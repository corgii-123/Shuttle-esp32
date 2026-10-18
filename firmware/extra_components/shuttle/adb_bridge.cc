#include "shuttle/adb_bridge.h"

#include <esp_crt_bundle.h>
#include <esp_heap_caps.h>
#include <esp_log.h>
#include <esp_timer.h>
#include <esp_websocket_client.h>
#include <freertos/idf_additions.h>
#include <lwip/netdb.h>
#include <lwip/sockets.h>
#include <sdkconfig.h>

#include <algorithm>
#include <cerrno>
#include <cstring>

#include "shuttle/shuttle_config.h"

#define TAG "AdbBridge"

namespace shuttle {

namespace {

constexpr EventBits_t kEvtNetworkChanged = BIT0;
constexpr EventBits_t kEvtWsConnected = BIT1;
constexpr EventBits_t kEvtWsClosed = BIT2;
constexpr EventBits_t kEvtConnectReply = BIT3;     // server answered the hello
constexpr EventBits_t kEvtCandidatesChanged = BIT4;
constexpr EventBits_t kEvtRescan = BIT5;           // scanner: scan now
constexpr EventBits_t kEvtScanNetwork = BIT6;      // scanner: network up/down

constexpr int kCloseSwitchPhone = 4005;  // server: reconnect now and ask again
constexpr int kHelloReplyTimeoutMs = 3000;
constexpr int kNoPhoneRescanS = 15;
constexpr int64_t kScanQuietUs = 5LL * 1000 * 1000;  // no ADB traffic for this long before a scan

constexpr int kWsConnectTimeoutMs = 20000;
constexpr int kPhoneConnectTimeoutMs = 5000;
constexpr int kStableSessionMs = 10000;  // sessions longer than this reset the backoff

int64_t NowMs() { return esp_timer_get_time() / 1000; }

std::string JsonEscape(const std::string& in) {
    std::string out;
    for (char c : in) {
        if (c == '"' || c == '\\') {
            out += '\\';
            out += c;
        } else if (static_cast<unsigned char>(c) < 0x20) {
            char buf[8];
            snprintf(buf, sizeof(buf), "\\u%04x", c);
            out += buf;
        } else {
            out += c;
        }
    }
    return out;
}

// Minimal reader for the server's flat control messages: the string value of "key".
std::string JsonString(const std::string& msg, const char* key) {
    std::string pattern = std::string("\"") + key + "\"";
    size_t pos = msg.find(pattern);
    if (pos == std::string::npos) {
        return "";
    }
    pos += pattern.size();
    while (pos < msg.size() && (msg[pos] == ' ' || msg[pos] == ':')) {
        pos++;
    }
    if (pos >= msg.size() || msg[pos] != '"') {
        return "";
    }
    std::string out;
    for (pos++; pos < msg.size() && msg[pos] != '"'; pos++) {
        if (msg[pos] == '\\' && pos + 1 < msg.size()) {
            pos++;
        }
        out += msg[pos];
    }
    return out;
}

std::string JsonArray(const std::vector<std::string>& items) {
    std::string out = "[";
    for (size_t i = 0; i < items.size(); i++) {
        out += (i ? ",\"" : "\"") + JsonEscape(items[i]) + "\"";
    }
    return out + "]";
}

bool Contains(const std::vector<std::string>& v, const std::string& s) {
    return std::find(v.begin(), v.end(), s) != v.end();
}

uint8_t* AllocPsram(size_t size) {
    auto* p = static_cast<uint8_t*>(heap_caps_malloc(size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (p == nullptr) {
        ESP_LOGW(TAG, "PSRAM alloc of %u bytes failed, falling back to internal RAM", (unsigned)size);
        p = static_cast<uint8_t*>(heap_caps_malloc(size, MALLOC_CAP_8BIT));
    }
    return p;
}

}  // namespace

const char* LinkStateName(LinkState state) {
    switch (state) {
        case LinkState::kDisabled: return "disabled";
        case LinkState::kWaitingNetwork: return "waiting_network";
        case LinkState::kScanning: return "scanning";
        case LinkState::kNoPhone: return "no_phone";
        case LinkState::kConnectingServer: return "connecting_server";
        case LinkState::kConnectingPhone: return "connecting_phone";
        case LinkState::kPhoneUnreachable: return "phone_unreachable";
        case LinkState::kServerUnreachable: return "server_unreachable";
        case LinkState::kActive: return "active";
    }
    return "unknown";
}

AdbBridge::Config AdbBridge::DefaultConfig() {
    Config c;
    c.url = GetAdbUrl();
    c.token = GetToken();
    c.phone_port = CONFIG_SHUTTLE_ADB_PHONE_PORT;
    c.rx_buffer_bytes = CONFIG_SHUTTLE_ADB_RX_BUFFER_KB * 1024;
    c.tx_chunk_bytes = CONFIG_SHUTTLE_ADB_TX_CHUNK_KB * 1024;
    c.ws_buffer_size = CONFIG_SHUTTLE_ADB_WS_BUFFER_SIZE;
    c.backoff_min_ms = CONFIG_SHUTTLE_BACKOFF_MIN_MS;
    c.backoff_max_ms = CONFIG_SHUTTLE_BACKOFF_MAX_MS;
    c.ping_interval_s = CONFIG_SHUTTLE_ADB_PING_INTERVAL_S;
    c.pong_timeout_s = CONFIG_SHUTTLE_ADB_PONG_TIMEOUT_S;
    return c;
}

AdbBridge::AdbBridge(Config config, TargetResolver resolver)
    : config_(std::move(config)),
      resolver_(std::move(resolver)),
      scanner_([this]() {
          auto o = PhoneScanner::DefaultOptions();
          o.port = config_.phone_port;
          return o;
      }()),
      server_backoff_(config_.backoff_min_ms, config_.backoff_max_ms),
      phone_backoff_(std::max(3000, config_.backoff_min_ms), config_.backoff_max_ms) {
    events_ = xEventGroupCreate();
    writer_mutex_ = xSemaphoreCreateMutex();

    headers_ = "Authorization: Bearer " + config_.token + "\r\n";
    if (!config_.device_id.empty()) {
        headers_ += "Device-Id: " + config_.device_id + "\r\n";
    }
    headers_ += "X-Shuttle-Channel: adb\r\n";
}

AdbBridge::~AdbBridge() {
    // The bridge is a process-lifetime singleton on the device; tasks are not joined.
    if (rx_stream_ != nullptr) {
        vStreamBufferDeleteWithCaps(rx_stream_);
    }
    heap_caps_free(tx_chunk_);
    heap_caps_free(writer_buf_);
    vSemaphoreDelete(writer_mutex_);
    vEventGroupDelete(events_);
}

bool AdbBridge::Start() {
    if (bridge_task_ != nullptr) {
        return true;
    }
    // Buffers live in PSRAM: the bridge may hold a few hundred KB of ADB traffic
    // (screenshots) while the phone or the server catches up.
    rx_stream_ = xStreamBufferCreateWithCaps(config_.rx_buffer_bytes, 1, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    tx_chunk_ = AllocPsram(config_.tx_chunk_bytes);
    writer_buf_ = AllocPsram(config_.tx_chunk_bytes);
    if (rx_stream_ == nullptr || tx_chunk_ == nullptr || writer_buf_ == nullptr) {
        ESP_LOGE(TAG, "Failed to allocate bridge buffers");
        return false;
    }
    ESP_LOGI(TAG, "Starting ADB bridge: %s (phone port %d, rx buffer %uKB)", config_.url.c_str(),
             config_.phone_port, (unsigned)(config_.rx_buffer_bytes / 1024));
    xTaskCreate(WriterTaskEntry, "adb_tx", 4096, this, 5, &writer_task_);
    xTaskCreate(BridgeTaskEntry, "adb_bridge", 6144, this, 4, &bridge_task_);
    xTaskCreate(ScanTaskEntry, "adb_scan", 4096, this, 3, &scan_task_);
    return true;
}

void AdbBridge::SetNetworkUp(bool up) {
    bool was = network_up_.exchange(up);
    if (was != up) {
        ESP_LOGI(TAG, "Network %s", up ? "up" : "down");
        if (!up) {
            scan_abort_ = true;
        }
        xEventGroupSetBits(events_, kEvtNetworkChanged | kEvtScanNetwork);
    }
}

void AdbBridge::RequestRescan() { xEventGroupSetBits(events_, kEvtRescan); }

std::vector<std::string> AdbBridge::GetCandidates() {
    std::lock_guard<std::mutex> lock(cand_mutex_);
    return candidates_;
}

void AdbBridge::DropCandidate(const std::string& host) {
    std::lock_guard<std::mutex> lock(cand_mutex_);
    candidates_.erase(std::remove(candidates_.begin(), candidates_.end(), host), candidates_.end());
}

void AdbBridge::SetStatusCallback(StatusCallback callback) {
    std::lock_guard<std::mutex> lock(status_mutex_);
    status_callback_ = std::move(callback);
}

AdbBridgeStatus AdbBridge::GetStatus() {
    std::lock_guard<std::mutex> lock(status_mutex_);
    return status_;
}

void AdbBridge::SetState(LinkState state) {
    StatusCallback cb;
    AdbBridgeStatus snapshot;
    {
        std::lock_guard<std::mutex> lock(status_mutex_);
        if (status_.state == state) {
            return;
        }
        ESP_LOGI(TAG, "State %s -> %s", LinkStateName(status_.state), LinkStateName(state));
        status_.state = state;
        snapshot = status_;
        cb = status_callback_;
    }
    if (cb) {
        cb(snapshot);
    }
}

void AdbBridge::DelayInterruptible(int ms) {
    // Wake early when the network changes so we react quickly to Wi-Fi coming back.
    xEventGroupClearBits(events_, kEvtNetworkChanged);
    xEventGroupWaitBits(events_, kEvtNetworkChanged, pdTRUE, pdFALSE, pdMS_TO_TICKS(ms));
}

void AdbBridge::BridgeTaskEntry(void* arg) { static_cast<AdbBridge*>(arg)->BridgeTask(); }

void AdbBridge::WriterTaskEntry(void* arg) { static_cast<AdbBridge*>(arg)->WriterTask(); }

void AdbBridge::ScanTaskEntry(void* arg) { static_cast<AdbBridge*>(arg)->ScanTask(); }

void AdbBridge::ScanTask() {
    while (true) {
        if (!network_up_) {
            {
                std::lock_guard<std::mutex> lock(cand_mutex_);
                candidates_.clear();
                scanned_ = false;
            }
            xEventGroupWaitBits(events_, kEvtScanNetwork, pdTRUE, pdFALSE, portMAX_DELAY);
            continue;
        }
        scan_abort_ = false;
        xEventGroupClearBits(events_, kEvtRescan | kEvtScanNetwork);
        Target target = resolver_ ? resolver_() : Target{};
        std::vector<std::string> found;
        int wait_s = 0;  // 0 = until asked / network change
        if (target.mode == Target::Mode::kFixed) {
            found.push_back(target.fixed_host);
        } else if (target.mode == Target::Mode::kAuto) {
            uint32_t ip = 0, mask = 0, gw = 0;
            if (!GetStaIpv4(&ip, &mask, &gw)) {
                vTaskDelay(pdMS_TO_TICKS(1000));
                continue;
            }
            std::vector<std::string> first;
            std::string in_use;
            {
                std::lock_guard<std::mutex> lock(cand_mutex_);
                in_use = current_host_;
                first = candidates_;
            }
            // Probing the whole /24 takes ~20 s of air time: not in the middle of a transfer.
            while (!in_use.empty() && network_up_ && !scan_abort_ &&
                   esp_timer_get_time() - last_traffic_us_.load() < kScanQuietUs) {
                vTaskDelay(pdMS_TO_TICKS(1000));
            }
            // the phone in use, the one used last time here, the hotspot phone, then the rest
            first.insert(first.begin(), {in_use, LoadLastPhone(target.ssid), Ipv4ToString(gw)});
            found = scanner_.Scan(ip, mask, first, in_use, scan_abort_);
            if (scan_abort_) {
                continue;  // network changed mid-scan
            }
            // No phone yet: look again soon. One in use (it is not probed, so `found` can be
            // empty): only now and then, for a phone that just ran 'adb tcpip 5555'.
            wait_s = in_use.empty() && found.empty() ? kNoPhoneRescanS : CONFIG_SHUTTLE_PHONE_SCAN_INTERVAL_S;
        }
        bool changed;
        {
            std::lock_guard<std::mutex> lock(cand_mutex_);
            changed = found != candidates_ || !scanned_;
            candidates_ = found;
            scanned_ = true;
        }
        {
            std::lock_guard<std::mutex> lock(status_mutex_);
            status_.phones_found = found.size();
        }
        if (changed) {
            ESP_LOGI(TAG, "Phones on '%s' (%s): %d", target.ssid.c_str(),
                     target.mode == Target::Mode::kFixed ? "fixed" : "scan", (int)found.size());
            xEventGroupSetBits(events_, kEvtCandidatesChanged);
        }
        xEventGroupWaitBits(events_, kEvtRescan | kEvtScanNetwork, pdFALSE, pdFALSE,
                            wait_s > 0 ? pdMS_TO_TICKS(wait_s * 1000) : portMAX_DELAY);
    }
}

void AdbBridge::BridgeTask() {
    while (true) {
        if (!network_up_) {
            SetState(LinkState::kWaitingNetwork);
            xEventGroupWaitBits(events_, kEvtNetworkChanged, pdTRUE, pdFALSE, portMAX_DELAY);
            continue;
        }

        Target target = resolver_ ? resolver_() : Target{};
        if (target.mode == Target::Mode::kDisabled) {
            SetState(LinkState::kDisabled);
            DelayInterruptible(10000);
            continue;
        }
        auto candidates = GetCandidates();
        if (candidates.empty()) {
            bool scanned;
            {
                std::lock_guard<std::mutex> lock(cand_mutex_);
                scanned = scanned_;
            }
            SetState(scanned ? LinkState::kNoPhone : LinkState::kScanning);
            xEventGroupWaitBits(events_, kEvtCandidatesChanged | kEvtNetworkChanged, pdTRUE, pdFALSE,
                                pdMS_TO_TICKS(10000));
            continue;
        }
        {
            std::lock_guard<std::mutex> lock(status_mutex_);
            status_.gateway = GetStaGatewayIp();
        }

        SetState(LinkState::kConnectingServer);
        if (!OpenWebSocket()) {
            SetState(LinkState::kServerUnreachable);
            int delay = server_backoff_.Next();
            ESP_LOGW(TAG, "Server connect failed (attempt %d), retry in %d ms", server_backoff_.attempts(), delay);
            DelayInterruptible(delay);
            continue;
        }
        server_backoff_.Reset();

        std::string host = NegotiateHost(target, candidates);
        if (host.empty()) {
            ESP_LOGI(TAG, "Server has no phone for us, rescanning");
            CloseWebSocket(1000, "no phone");
            RequestRescan();
            DelayInterruptible(5000);
            continue;
        }
        {
            std::lock_guard<std::mutex> lock(status_mutex_);
            if (status_.phone_host != host) {
                status_.phone_name.clear();  // another phone: name comes again from the server
            }
            status_.phone_host = host;
        }

        SetState(LinkState::kConnectingPhone);
        int sock = ConnectPhone(host);
        if (sock < 0) {
            SetState(LinkState::kPhoneUnreachable);
            SendControl("unreachable", host, "connect " + host + ":" + std::to_string(config_.phone_port) + " failed");
            CloseWebSocket(4001, "phone unreachable");
            DropCandidate(host);
            RequestRescan();
            int delay = phone_backoff_.Next();
            ESP_LOGW(TAG, "Phone %s:%d unreachable (attempt %d), rescanning; retry in %d ms", host.c_str(),
                     config_.phone_port, phone_backoff_.attempts(), delay);
            DelayInterruptible(delay);
            continue;
        }

        if (target.mode == Target::Mode::kAuto) {
            SaveLastPhone(target.ssid, host);
        }
        {
            std::lock_guard<std::mutex> lock(cand_mutex_);
            current_host_ = host;
        }
        int64_t started = NowMs();
        {
            std::lock_guard<std::mutex> lock(status_mutex_);
            status_.sessions++;
        }
        SendControl("connected", host, host + ":" + std::to_string(config_.phone_port));
        SetState(LinkState::kActive);
        RunSession(sock);
        int64_t lasted = NowMs() - started;
        {
            std::lock_guard<std::mutex> lock(cand_mutex_);
            current_host_.clear();
        }
        EndSession();
        CloseWebSocket(1000, "session end");

        if (switch_requested_.exchange(false) || server_close_code_ == kCloseSwitchPhone) {
            ESP_LOGI(TAG, "Server asked to switch phone, reconnecting now");
            phone_backoff_.Reset();
            DelayInterruptible(100);
        } else if (lasted >= kStableSessionMs) {
            phone_backoff_.Reset();
            DelayInterruptible(300);
        } else {
            int delay = phone_backoff_.Next();
            ESP_LOGW(TAG, "Session lasted only %lld ms, retry in %d ms", lasted, delay);
            RequestRescan();  // the phone may be gone
            DelayInterruptible(delay);
        }
    }
}

std::string AdbBridge::NegotiateHost(const Target& target, const std::vector<std::string>& candidates) {
    std::string gateway = GetStaGatewayIp();
    std::string last = LoadLastPhone(target.ssid);
    xEventGroupClearBits(events_, kEvtConnectReply);
    // Hello: what we can reach; the server knows the phones (by serial) and picks one.
    std::string hello = "{\"type\":\"hello\",\"v\":2,\"ssid\":\"" + JsonEscape(target.ssid) +
                        "\",\"ip\":\"" + GetStaIp() + "\",\"gateway\":\"" + gateway +
                        "\",\"candidates\":" + JsonArray(candidates) + ",\"last\":\"" + JsonEscape(last) +
                        "\",\"fixed\":" + (target.mode == Target::Mode::kFixed ? "true" : "false") + "}";
    SendText(hello);
    EventBits_t bits = xEventGroupWaitBits(events_, kEvtConnectReply | kEvtWsClosed, pdFALSE, pdFALSE,
                                           pdMS_TO_TICKS(kHelloReplyTimeoutMs));
    if (bits & kEvtConnectReply) {
        std::lock_guard<std::mutex> lock(cand_mutex_);
        ESP_LOGI(TAG, "Server chose phone %s", server_host_.empty() ? "(none)" : server_host_.c_str());
        return server_host_;
    }
    if (bits & kEvtWsClosed) {
        return "";
    }
    // Older server without phone selection: our own choice.
    std::string host = Contains(candidates, last)      ? last
                       : Contains(candidates, gateway) ? gateway
                                                       : candidates.front();
    ESP_LOGI(TAG, "No answer to hello, using phone %s", host.c_str());
    return host;
}

void AdbBridge::SendCandidates(const std::vector<std::string>& candidates) {
    Target target = resolver_ ? resolver_() : Target{};
    SendText("{\"type\":\"candidates\",\"ssid\":\"" + JsonEscape(target.ssid) +
             "\",\"candidates\":" + JsonArray(candidates) + "}");
}

void AdbBridge::HandleServerText(const std::string& msg) {
    // Runs in the WebSocket task: only record and signal.
    std::string type = JsonString(msg, "type");
    if (type == "connect" || type == "idle") {
        {
            std::lock_guard<std::mutex> lock(cand_mutex_);
            server_host_ = type == "connect" ? JsonString(msg, "host") : "";
        }
        xEventGroupSetBits(events_, kEvtConnectReply);
    } else if (type == "rescan") {
        RequestRescan();
    } else if (type == "switch") {
        switch_requested_ = true;
    } else if (type == "phone_info") {
        StatusCallback cb;
        AdbBridgeStatus snapshot;
        {
            std::lock_guard<std::mutex> lock(status_mutex_);
            status_.phone_name = JsonString(msg, "name");
            snapshot = status_;
            cb = status_callback_;
        }
        ESP_LOGI(TAG, "Phone is '%s'", snapshot.phone_name.c_str());
        if (cb) {
            cb(snapshot);
        }
    }
}

void AdbBridge::SetServerUrl(const std::string& url) {
    std::lock_guard<std::mutex> lock(status_mutex_);
    next_url_ = url;
}

bool AdbBridge::OpenWebSocket() {
    {
        std::lock_guard<std::mutex> lock(status_mutex_);
        if (!next_url_.empty() && next_url_ != config_.url) {
            ESP_LOGI(TAG, "Server URL for this network: %s", next_url_.c_str());
            config_.url = next_url_;
        }
    }
    xEventGroupClearBits(events_, kEvtWsConnected | kEvtWsClosed | kEvtConnectReply);
    last_frame_binary_ = false;
    server_close_code_ = 0;
    switch_requested_ = false;

    esp_websocket_client_config_t cfg = {};
    cfg.uri = config_.url.c_str();
    cfg.headers = headers_.c_str();
    cfg.buffer_size = config_.ws_buffer_size;
    cfg.disable_auto_reconnect = true;  // we own reconnect/backoff
    cfg.ping_interval_sec = config_.ping_interval_s;
    cfg.pingpong_timeout_sec = config_.pong_timeout_s;
    cfg.network_timeout_ms = 15000;
    cfg.task_name = "adb_ws";
    cfg.task_stack = 6144;
    cfg.task_prio = 5;
    cfg.keep_alive_enable = true;
    cfg.keep_alive_idle = 15;
    cfg.keep_alive_interval = 5;
    cfg.keep_alive_count = 3;
    if (config_.url.rfind("wss://", 0) == 0) {
        cfg.crt_bundle_attach = esp_crt_bundle_attach;
    }

    ws_ = esp_websocket_client_init(&cfg);
    if (ws_ == nullptr) {
        ESP_LOGE(TAG, "esp_websocket_client_init failed");
        return false;
    }
    esp_websocket_register_events(ws_, WEBSOCKET_EVENT_ANY, reinterpret_cast<esp_event_handler_t>(WsEventHandler),
                                  this);
    ESP_LOGI(TAG, "Connecting %s", config_.url.c_str());
    if (esp_websocket_client_start(ws_) != ESP_OK) {
        ESP_LOGE(TAG, "esp_websocket_client_start failed");
        esp_websocket_client_destroy(ws_);
        ws_ = nullptr;
        return false;
    }
    EventBits_t bits = xEventGroupWaitBits(events_, kEvtWsConnected | kEvtWsClosed, pdFALSE, pdFALSE,
                                           pdMS_TO_TICKS(kWsConnectTimeoutMs));
    if (!(bits & kEvtWsConnected) || (bits & kEvtWsClosed)) {
        ESP_LOGW(TAG, "WebSocket connect %s", (bits & kEvtWsClosed) ? "rejected/failed" : "timeout");
        CloseWebSocket(1000, "connect failed");
        return false;
    }
    ESP_LOGI(TAG, "WebSocket connected");
    return true;
}

void AdbBridge::CloseWebSocket(int code, const char* reason) {
    accept_rx_ = false;
    if (ws_ == nullptr) {
        return;
    }
    if (esp_websocket_client_is_connected(ws_)) {
        esp_websocket_client_close_with_code(ws_, code, reason, strlen(reason), pdMS_TO_TICKS(2000));
    }
    // destroy() stops the client task; never call it from the WS event handler.
    esp_websocket_client_destroy(ws_);
    ws_ = nullptr;
}

void AdbBridge::SendText(const std::string& msg) {
    if (ws_ == nullptr || !esp_websocket_client_is_connected(ws_)) {
        return;
    }
    // Text frames carry control messages; binary frames carry ADB bytes only.
    esp_websocket_client_send_text(ws_, msg.c_str(), msg.size(), pdMS_TO_TICKS(2000));
}

void AdbBridge::SendControl(const char* state, const std::string& host, const std::string& detail) {
    SendText(std::string("{\"type\":\"phone\",\"state\":\"") + state + "\",\"host\":\"" + JsonEscape(host) +
             "\",\"detail\":\"" + JsonEscape(detail) + "\"}");
}

int AdbBridge::ConnectPhone(const std::string& host) {
    struct addrinfo hints = {};
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    struct addrinfo* res = nullptr;
    std::string port = std::to_string(config_.phone_port);
    if (getaddrinfo(host.c_str(), port.c_str(), &hints, &res) != 0 || res == nullptr) {
        ESP_LOGW(TAG, "Cannot resolve phone host %s", host.c_str());
        return -1;
    }

    int sock = socket(res->ai_family, res->ai_socktype, res->ai_protocol);
    if (sock < 0) {
        freeaddrinfo(res);
        return -1;
    }
    int flags = fcntl(sock, F_GETFL, 0);
    fcntl(sock, F_SETFL, flags | O_NONBLOCK);
    int rc = connect(sock, res->ai_addr, res->ai_addrlen);
    freeaddrinfo(res);
    if (rc != 0 && errno != EINPROGRESS) {
        close(sock);
        return -1;
    }
    if (rc != 0) {
        fd_set wfds;
        FD_ZERO(&wfds);
        FD_SET(sock, &wfds);
        struct timeval tv = {.tv_sec = kPhoneConnectTimeoutMs / 1000, .tv_usec = 0};
        if (select(sock + 1, nullptr, &wfds, nullptr, &tv) <= 0) {
            close(sock);
            return -1;
        }
        int err = 0;
        socklen_t len = sizeof(err);
        getsockopt(sock, SOL_SOCKET, SO_ERROR, &err, &len);
        if (err != 0) {
            ESP_LOGW(TAG, "Phone connect error: %s", strerror(err));
            close(sock);
            return -1;
        }
    }
    fcntl(sock, F_SETFL, flags & ~O_NONBLOCK);

    int one = 1;
    setsockopt(sock, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    setsockopt(sock, SOL_SOCKET, SO_KEEPALIVE, &one, sizeof(one));
    int idle = 10, intvl = 5, cnt = 3;
    setsockopt(sock, IPPROTO_TCP, TCP_KEEPIDLE, &idle, sizeof(idle));
    setsockopt(sock, IPPROTO_TCP, TCP_KEEPINTVL, &intvl, sizeof(intvl));
    setsockopt(sock, IPPROTO_TCP, TCP_KEEPCNT, &cnt, sizeof(cnt));
    struct timeval snd_timeout = {.tv_sec = 15, .tv_usec = 0};
    setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &snd_timeout, sizeof(snd_timeout));
    ESP_LOGI(TAG, "Phone connected %s:%d (fd %d)", host.c_str(), config_.phone_port, sock);
    return sock;
}

void AdbBridge::RunSession(int sock) {
    writer_error_ = false;
    session_sock_ = sock;  // writer task starts draining the stream buffer
    while (true) {
        if (xEventGroupGetBits(events_) & kEvtWsClosed) {
            ESP_LOGI(TAG, "Server side closed");
            break;
        }
        if (!network_up_) {
            ESP_LOGI(TAG, "Network lost");
            break;
        }
        if (writer_error_) {
            ESP_LOGI(TAG, "Phone write failed");
            break;
        }
        if (switch_requested_) {
            ESP_LOGI(TAG, "Switching phone");
            break;
        }
        if (xEventGroupGetBits(events_) & kEvtCandidatesChanged) {
            xEventGroupClearBits(events_, kEvtCandidatesChanged);
            SendCandidates(GetCandidates());
        }
        fd_set rfds;
        FD_ZERO(&rfds);
        FD_SET(sock, &rfds);
        struct timeval tv = {.tv_sec = 0, .tv_usec = 200 * 1000};
        int ready = select(sock + 1, &rfds, nullptr, nullptr, &tv);
        if (ready < 0) {
            ESP_LOGW(TAG, "select() failed: %d", errno);
            break;
        }
        if (ready == 0) {
            continue;
        }
        int n = recv(sock, tx_chunk_, config_.tx_chunk_bytes, 0);
        if (n <= 0) {
            ESP_LOGI(TAG, "Phone closed the connection (%d, errno %d)", n, n < 0 ? errno : 0);
            break;
        }
        int sent = esp_websocket_client_send_bin(ws_, reinterpret_cast<const char*>(tx_chunk_), n,
                                                 pdMS_TO_TICKS(15000));
        if (sent != n) {
            ESP_LOGW(TAG, "WebSocket send failed (%d/%d)", sent, n);
            break;
        }
        last_traffic_us_ = esp_timer_get_time();
        std::lock_guard<std::mutex> lock(status_mutex_);
        status_.bytes_from_phone += n;
    }
}

void AdbBridge::EndSession() {
    accept_rx_ = false;
    // Take the writer lock so the writer is not mid-send on the socket we close.
    xSemaphoreTake(writer_mutex_, portMAX_DELAY);
    int sock = session_sock_.exchange(-1);
    if (sock >= 0) {
        shutdown(sock, SHUT_RDWR);
        close(sock);
    }
    // Drop bytes that belonged to this session; the next one starts with a fresh CNXN.
    size_t dropped = 0;
    while (true) {
        size_t n = xStreamBufferReceive(rx_stream_, writer_buf_, config_.tx_chunk_bytes, 0);
        if (n == 0) {
            break;
        }
        dropped += n;
    }
    xSemaphoreGive(writer_mutex_);
    auto st = GetStatus();
    ESP_LOGI(TAG, "Session ended (to phone %llu B, from phone %llu B, dropped %u B)", st.bytes_to_phone,
             st.bytes_from_phone, (unsigned)dropped);
}

void AdbBridge::WriterTask() {
    while (true) {
        if (session_sock_ < 0) {
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        xSemaphoreTake(writer_mutex_, portMAX_DELAY);
        int sock = session_sock_;
        if (sock < 0) {
            xSemaphoreGive(writer_mutex_);
            continue;
        }
        size_t n = xStreamBufferReceive(rx_stream_, writer_buf_, config_.tx_chunk_bytes, pdMS_TO_TICKS(50));
        size_t off = 0;
        while (off < n) {
            int w = send(sock, writer_buf_ + off, n - off, 0);
            if (w <= 0) {
                ESP_LOGW(TAG, "send() to phone failed: errno %d", errno);
                writer_error_ = true;
                break;
            }
            off += w;
        }
        xSemaphoreGive(writer_mutex_);
        if (off > 0) {
            last_traffic_us_ = esp_timer_get_time();
            std::lock_guard<std::mutex> lock(status_mutex_);
            status_.bytes_to_phone += off;
        }
        if (writer_error_) {
            vTaskDelay(pdMS_TO_TICKS(50));
        }
    }
}

void AdbBridge::WsEventHandler(void* arg, const char* base, int32_t event_id, void* event_data) {
    auto* self = static_cast<AdbBridge*>(arg);
    auto* data = static_cast<esp_websocket_event_data_t*>(event_data);
    switch (event_id) {
        case WEBSOCKET_EVENT_CONNECTED:
            // Accept server bytes immediately: the Mac's adb may send CNXN before the
            // phone TCP connect completes; they wait in the PSRAM stream buffer.
            self->accept_rx_ = true;
            xEventGroupSetBits(self->events_, kEvtWsConnected);
            break;
        case WEBSOCKET_EVENT_DISCONNECTED:
        case WEBSOCKET_EVENT_CLOSED:
            self->accept_rx_ = false;
            xEventGroupSetBits(self->events_, kEvtWsClosed);
            break;
        case WEBSOCKET_EVENT_ERROR:
            if (data != nullptr) {
                ESP_LOGW(TAG, "WebSocket error type %d, http %d, tls 0x%x, errno %d", data->error_handle.error_type,
                         data->error_handle.esp_ws_handshake_status_code, data->error_handle.esp_tls_last_esp_err,
                         data->error_handle.esp_transport_sock_errno);
                if (data->error_handle.esp_ws_handshake_status_code == 401) {
                    ESP_LOGE(TAG, "Server rejected token (HTTP 401) - check CONFIG_SHUTTLE_TOKEN");
                }
            }
            break;
        case WEBSOCKET_EVENT_DATA: {
            if (data == nullptr || data->data_len <= 0) {
                break;
            }
            bool binary = data->op_code == 0x2 || (data->op_code == 0x0 && self->last_frame_binary_);
            if (data->op_code == 0x2 || data->op_code == 0x1) {
                self->last_frame_binary_ = (data->op_code == 0x2);
            }
            if (data->op_code == 0x8 && data->data_len >= 2) {
                auto* p = reinterpret_cast<const uint8_t*>(data->data_ptr);
                self->server_close_code_ = (p[0] << 8) | p[1];
                ESP_LOGI(TAG, "Server closed with code %d", self->server_close_code_.load());
                break;
            }
            if (data->op_code == 0x1) {
                ESP_LOGI(TAG, "Server control: %.*s", data->data_len > 160 ? 160 : data->data_len, data->data_ptr);
                if (data->payload_offset == 0 && data->data_len == data->payload_len) {
                    self->HandleServerText(std::string(data->data_ptr, data->data_len));
                }
                break;
            }
            if (!binary) {
                break;  // ping/pong/close
            }
            const char* p = data->data_ptr;
            size_t left = data->data_len;
            // Backpressure: block the WS task while the phone drains the buffer.
            while (left > 0 && self->accept_rx_) {
                size_t w = xStreamBufferSend(self->rx_stream_, p, left, pdMS_TO_TICKS(100));
                p += w;
                left -= w;
            }
            break;
        }
        default:
            break;
    }
}

}  // namespace shuttle
