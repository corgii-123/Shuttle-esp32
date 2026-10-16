#include "shuttle/phone_scanner.h"

#include <esp_log.h>
#include <esp_timer.h>
#include <lwip/inet.h>
#include <lwip/sockets.h>
#include <sdkconfig.h>

#include <algorithm>
#include <cerrno>
#include <cstring>

#include "shuttle/shuttle_config.h"

#define TAG "PhoneScanner"

namespace shuttle {

namespace {

constexpr uint32_t kCmdCnxn = 0x4e584e43;  // "CNXN"
constexpr uint32_t kCmdAuth = 0x48545541;  // "AUTH"
constexpr uint32_t kCmdStls = 0x534c5453;  // "STLS"
constexpr uint32_t kAdbVersion = 0x01000001;
constexpr uint32_t kMaxPayload = 256 * 1024;

int64_t NowMs() { return esp_timer_get_time() / 1000; }

uint32_t ParseIpv4(const std::string& host) {
    struct in_addr a = {};
    if (inet_aton(host.c_str(), &a) == 0) {
        return 0;
    }
    return a.s_addr;
}

void PutLe32(uint8_t* p, uint32_t v) {
    p[0] = v & 0xff;
    p[1] = (v >> 8) & 0xff;
    p[2] = (v >> 16) & 0xff;
    p[3] = (v >> 24) & 0xff;
}

uint32_t GetLe32(const uint8_t* p) { return p[0] | (p[1] << 8) | (p[2] << 16) | (uint32_t(p[3]) << 24); }

}  // namespace

PhoneScanner::Options PhoneScanner::DefaultOptions() {
    Options o;
    o.port = CONFIG_SHUTTLE_ADB_PHONE_PORT;
    o.batch = CONFIG_SHUTTLE_PHONE_SCAN_BATCH;
    o.connect_timeout_ms = CONFIG_SHUTTLE_PHONE_SCAN_TIMEOUT_MS;
    return o;
}

void PhoneScanner::ConnectBatch(const std::vector<uint32_t>& addrs, std::vector<std::pair<uint32_t, int>>* open) {
    std::vector<std::pair<uint32_t, int>> pending;
    for (uint32_t addr : addrs) {
        int sock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        if (sock < 0) {
            ESP_LOGW(TAG, "socket() failed (errno %d), raise CONFIG_LWIP_MAX_SOCKETS?", errno);
            break;
        }
        fcntl(sock, F_SETFL, fcntl(sock, F_GETFL, 0) | O_NONBLOCK);
        struct sockaddr_in sa = {};
        sa.sin_family = AF_INET;
        sa.sin_port = htons(options_.port);
        sa.sin_addr.s_addr = addr;
        int rc = connect(sock, reinterpret_cast<struct sockaddr*>(&sa), sizeof(sa));
        if (rc == 0) {
            open->emplace_back(addr, sock);
        } else if (errno == EINPROGRESS) {
            pending.emplace_back(addr, sock);
        } else {
            close(sock);
        }
    }

    int64_t deadline = NowMs() + options_.connect_timeout_ms;
    while (!pending.empty()) {
        int64_t left = deadline - NowMs();
        if (left <= 0) {
            break;
        }
        fd_set wfds;
        FD_ZERO(&wfds);
        int maxfd = -1;
        for (auto& [addr, sock] : pending) {
            FD_SET(sock, &wfds);
            maxfd = std::max(maxfd, sock);
        }
        struct timeval tv = {.tv_sec = static_cast<time_t>(left / 1000),
                             .tv_usec = static_cast<suseconds_t>((left % 1000) * 1000)};
        int n = select(maxfd + 1, nullptr, &wfds, nullptr, &tv);
        if (n <= 0) {
            break;
        }
        for (auto it = pending.begin(); it != pending.end();) {
            if (!FD_ISSET(it->second, &wfds)) {
                ++it;
                continue;
            }
            int err = 0;
            socklen_t len = sizeof(err);
            getsockopt(it->second, SOL_SOCKET, SO_ERROR, &err, &len);
            if (err == 0) {
                open->push_back(*it);
            } else {
                close(it->second);  // refused: a host without adbd
            }
            it = pending.erase(it);
        }
    }
    for (auto& [addr, sock] : pending) {
        close(sock);  // nobody answered in time (no such host, or it is asleep)
    }
}

bool PhoneScanner::Probe(int sock) {
    // A minimal ADB CNXN, as the adb host sends it; adbd answers AUTH (or CNXN / STLS).
    static const char kBanner[] = "host::";  // sent with its NUL
    uint8_t packet[24 + sizeof(kBanner)];
    uint32_t check = 0;
    for (size_t i = 0; i < sizeof(kBanner); i++) {
        check += static_cast<uint8_t>(kBanner[i]);
    }
    PutLe32(packet + 0, kCmdCnxn);
    PutLe32(packet + 4, kAdbVersion);
    PutLe32(packet + 8, kMaxPayload);
    PutLe32(packet + 12, sizeof(kBanner));
    PutLe32(packet + 16, check);
    PutLe32(packet + 20, kCmdCnxn ^ 0xffffffff);
    memcpy(packet + 24, kBanner, sizeof(kBanner));

    fcntl(sock, F_SETFL, fcntl(sock, F_GETFL, 0) & ~O_NONBLOCK);
    struct timeval tv = {.tv_sec = options_.probe_timeout_ms / 1000,
                         .tv_usec = (options_.probe_timeout_ms % 1000) * 1000};
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    if (send(sock, packet, sizeof(packet), 0) != static_cast<int>(sizeof(packet))) {
        return false;
    }
    uint8_t reply[24];
    size_t got = 0;
    int64_t deadline = NowMs() + options_.probe_timeout_ms;
    while (got < sizeof(reply) && NowMs() < deadline) {
        int n = recv(sock, reply + got, sizeof(reply) - got, 0);
        if (n <= 0) {
            return false;
        }
        got += n;
    }
    if (got < sizeof(reply)) {
        return false;
    }
    uint32_t cmd = GetLe32(reply);
    uint32_t magic = GetLe32(reply + 20);
    return (cmd == kCmdAuth || cmd == kCmdCnxn || cmd == kCmdStls) && magic == (cmd ^ 0xffffffff);
}

bool PhoneScanner::IsAdbd(const std::string& host) {
    uint32_t addr = ParseIpv4(host);
    if (addr == 0) {
        return false;
    }
    std::vector<std::pair<uint32_t, int>> open;
    ConnectBatch({addr}, &open);
    bool ok = false;
    for (auto& [a, sock] : open) {
        ok = Probe(sock);
        close(sock);
    }
    return ok;
}

std::vector<std::string> PhoneScanner::Scan(uint32_t ip, uint32_t netmask, const std::vector<std::string>& first,
                                            const std::string& in_use, const std::atomic<bool>& abort) {
    int64_t started = NowMs();
    // Host byte order for the arithmetic; at most a /24 around our own address.
    uint32_t h_ip = ntohl(ip);
    uint32_t h_mask = ntohl(netmask) | 0xffffff00;
    uint32_t net = h_ip & h_mask;
    uint32_t bcast = net | ~h_mask;

    std::vector<uint32_t> order;
    auto add = [&](uint32_t h) {
        if (h > net && h < bcast && h != h_ip && std::find(order.begin(), order.end(), h) == order.end()) {
            order.push_back(h);
        }
    };
    for (const auto& f : first) {
        uint32_t a = ParseIpv4(f);
        if (a != 0) {
            add(ntohl(a));
        }
    }
    size_t priority = order.size();
    for (uint32_t h = net + 1; h < bcast; h++) {
        add(h);
    }

    uint32_t in_use_addr = ParseIpv4(in_use);
    std::vector<std::string> found;
    int open_ports = 0;
    size_t batch = std::max(1, options_.batch);
    size_t i = 0;
    while (i < order.size() && !abort) {
        // the priority hosts get batches of their own, so they answer as early as possible
        size_t limit = i < priority ? priority : order.size();
        size_t end = std::min(limit, i + batch);
        std::vector<uint32_t> addrs;
        for (size_t j = i; j < end; j++) {
            addrs.push_back(htonl(order[j]));
        }
        i = end;
        std::vector<std::pair<uint32_t, int>> open;
        ConnectBatch(addrs, &open);
        for (auto& [addr, sock] : open) {
            open_ports++;
            bool adbd = (addr == in_use_addr) || Probe(sock);
            close(sock);
            std::string host = Ipv4ToString(addr);
            if (adbd) {
                found.push_back(host);
            } else {
                ESP_LOGI(TAG, "%s:%d is open but is not adbd", host.c_str(), options_.port);
            }
        }
    }
    // keep the priority hosts first (in their order), then by address
    std::vector<std::string> result;
    for (uint32_t h : order) {
        std::string s = Ipv4ToString(htonl(h));
        if (std::find(found.begin(), found.end(), s) != found.end()) {
            result.push_back(s);
        }
    }
    std::string list;
    for (const auto& r : result) {
        list += (list.empty() ? "" : ", ") + r;
    }
    ESP_LOGI(TAG, "Scan of %s/%d: %d open, %d phone(s) [%s] in %lld ms%s", Ipv4ToString(htonl(net)).c_str(),
             __builtin_popcount(h_mask), open_ports, (int)result.size(), list.c_str(), NowMs() - started,
             abort ? " (aborted)" : "");
    return result;
}

}  // namespace shuttle
