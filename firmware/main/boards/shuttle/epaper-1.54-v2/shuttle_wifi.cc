#include "shuttle_wifi.h"

#include <esp_log.h>
#include <freertos/task.h>
#include <nvs.h>
#include <ssid_manager.h>

#include <algorithm>
#include <cstring>

#include "shuttle/shuttle_config.h"
#include <sdkconfig.h>

#define TAG "ShuttleWifi"

namespace {
constexpr EventBits_t kConnected = BIT0;
constexpr EventBits_t kChanged = BIT1;
constexpr const char* kNvsNamespace = "shuttle";
constexpr const char* kNvsExtrasKey = "extra_ssids";
}  // namespace

ShuttleWifi::ShuttleWifi() { events_ = xEventGroupCreate(); }

void ShuttleWifi::LoadExtras() {
    extras_.clear();
    nvs_handle_t h;
    if (nvs_open(kNvsNamespace, NVS_READONLY, &h) != ESP_OK) {
        return;
    }
    size_t len = 0;
    if (nvs_get_str(h, kNvsExtrasKey, nullptr, &len) == ESP_OK && len > 0) {
        std::string buf(len, '\0');
        nvs_get_str(h, kNvsExtrasKey, buf.data(), &len);
        buf.resize(strnlen(buf.c_str(), buf.size()));
        // Format: "ssid\tpassword\n" per line (SSIDs cannot contain \t or \n in practice).
        size_t pos = 0;
        while (pos < buf.size()) {
            size_t nl = buf.find('\n', pos);
            std::string line = buf.substr(pos, nl == std::string::npos ? std::string::npos : nl - pos);
            pos = (nl == std::string::npos) ? buf.size() : nl + 1;
            size_t tab = line.find('\t');
            if (tab == std::string::npos || tab == 0) {
                continue;
            }
            extras_.push_back({line.substr(0, tab), line.substr(tab + 1), 0});
        }
    }
    nvs_close(h);
}

void ShuttleWifi::SaveExtras() {
    std::string buf;
    for (const auto& e : extras_) {
        buf += e.ssid + "\t" + e.password + "\n";
    }
    nvs_handle_t h;
    if (nvs_open(kNvsNamespace, NVS_READWRITE, &h) != ESP_OK) {
        return;
    }
    nvs_set_str(h, kNvsExtrasKey, buf.c_str());
    nvs_commit(h);
    nvs_close(h);
}

void ShuttleWifi::MergePortalNetworks() {
    // Anything in SsidManager that is neither from Kconfig nor already known came from
    // the xiaozhi Wi-Fi portal: remember it as a lower-priority extra.
    auto configured = shuttle::GetConfiguredNetworks();
    bool changed = false;
    for (const auto& item : SsidManager::GetInstance().GetSsidList()) {
        bool in_config = std::any_of(configured.begin(), configured.end(),
                                     [&](const shuttle::WifiNetwork& n) { return n.ssid == item.ssid; });
        if (in_config) {
            continue;
        }
        auto it = std::find_if(extras_.begin(), extras_.end(), [&](const Entry& e) { return e.ssid == item.ssid; });
        if (it == extras_.end()) {
            ESP_LOGI(TAG, "Remembering portal network '%s'", item.ssid.c_str());
            extras_.insert(extras_.begin(), {item.ssid, item.password, 0});  // newest first
            changed = true;
        } else if (it->password != item.password) {
            it->password = item.password;
            changed = true;
        }
    }
    if (changed) {
        SaveExtras();
    }
}

std::vector<ShuttleWifi::Entry> ShuttleWifi::BuildFullList() {
    std::vector<Entry> list;
    for (const auto& n : shuttle::GetConfiguredNetworks()) {
        list.push_back({n.ssid, n.password, static_cast<int>(list.size())});
    }
    for (const auto& e : extras_) {
        if (std::none_of(list.begin(), list.end(), [&](const Entry& x) { return x.ssid == e.ssid; })) {
            list.push_back({e.ssid, e.password, static_cast<int>(list.size())});
        }
    }
    if (list.size() > 10) {
        list.resize(10);  // SsidManager limit
    }
    return list;
}

void ShuttleWifi::ApplyList(const std::vector<Entry>& list) {
    auto& mgr = SsidManager::GetInstance();
    const auto& current = mgr.GetSsidList();
    bool same = current.size() == list.size();
    for (size_t i = 0; same && i < list.size(); i++) {
        same = current[i].ssid == list[i].ssid && current[i].password == list[i].password;
    }
    if (same) {
        return;
    }
    mgr.Clear();
    // AddSsid() inserts at the front, so add in reverse to keep priority order.
    for (auto it = list.rbegin(); it != list.rend(); ++it) {
        mgr.AddSsid(it->ssid, it->password);
    }
    std::string names;
    for (const auto& e : list) {
        names += (names.empty() ? "" : ", ") + e.ssid;
    }
    ESP_LOGI(TAG, "Station list: [%s]", names.c_str());
}

void ShuttleWifi::Init() {
    std::lock_guard<std::recursive_mutex> lock(mutex_);
    LoadExtras();
    MergePortalNetworks();
    full_ = BuildFullList();
    for (const auto& e : full_) {
        ESP_LOGI(TAG, "Network #%d: '%s'%s", e.priority + 1, e.ssid.c_str(),
                 e.priority < (int)shuttle::GetConfiguredNetworks().size() ? " (Kconfig)" : " (portal)");
    }
    ApplyList(full_);
}

bool ShuttleWifi::HasNetworks() {
    std::lock_guard<std::recursive_mutex> lock(mutex_);
    return !full_.empty();
}

int ShuttleWifi::PriorityOf(const std::string& ssid) {
    std::lock_guard<std::recursive_mutex> lock(mutex_);
    for (const auto& e : full_) {
        if (e.ssid == ssid) {
            return e.priority;
        }
    }
    return 99;
}

void ShuttleWifi::OnWifiEvent(WifiEvent event, const std::string& data) {
    switch (event) {
        case WifiEvent::Connected: {
            {
                std::lock_guard<std::recursive_mutex> lock(mutex_);
                connected_ssid_ = data;
            }
            ESP_LOGI(TAG, "Connected to '%s' (priority #%d)", data.c_str(), PriorityOf(data) + 1);
            xEventGroupSetBits(events_, kConnected | kChanged);
            break;
        }
        case WifiEvent::Disconnected:
            xEventGroupClearBits(events_, kConnected);
            xEventGroupSetBits(events_, kChanged);
            break;
        case WifiEvent::ConfigModeExit: {
            // User saved a network in the portal: keep it as a (lower priority) extra.
            std::lock_guard<std::recursive_mutex> lock(mutex_);
            MergePortalNetworks();
            full_ = BuildFullList();
            break;
        }
        default:
            break;
    }
}

void ShuttleWifi::RestartStation(const std::vector<Entry>* list) {
    // SsidManager has no lock and WifiStation copies its list while handling scan results,
    // so only rewrite the list while the station is stopped.
    auto& wifi = WifiManager::GetInstance();
    wifi.StopStation();
    if (list != nullptr) {
        std::lock_guard<std::recursive_mutex> lock(mutex_);
        ApplyList(*list);
    }
    wifi.StartStation();
}

bool ShuttleWifi::TryTier(int max_priority_exclusive, int timeout_s) {
    std::vector<Entry> tier;
    std::vector<Entry> full;
    {
        std::lock_guard<std::recursive_mutex> lock(mutex_);
        for (const auto& e : full_) {
            if (e.priority < max_priority_exclusive) {
                tier.push_back(e);
            }
        }
        full = full_;
    }
    if (tier.empty()) {
        return false;
    }
    ESP_LOGI(TAG, "Trying %d higher-priority network(s) for %ds (first: '%s')", (int)tier.size(), timeout_s,
             tier[0].ssid.c_str());
    xEventGroupClearBits(events_, kConnected);
    RestartStation(&tier);
    EventBits_t bits = xEventGroupWaitBits(events_, kConnected, pdFALSE, pdFALSE, pdMS_TO_TICKS(timeout_s * 1000));
    if (bits & kConnected) {
        // Connected: the station does not scan now, so restoring the full list is safe and
        // lets later reconnects use every network.
        std::lock_guard<std::recursive_mutex> lock(mutex_);
        ApplyList(full);
        return true;
    }
    ESP_LOGI(TAG, "Higher-priority networks not available, falling back to full list");
    RestartStation(&full);
    return false;
}

void ShuttleWifi::Start(std::function<bool()> is_idle) {
    std::lock_guard<std::recursive_mutex> lock(mutex_);
    is_idle_ = std::move(is_idle);
    if (task_started_) {
        return;
    }
    task_started_ = true;
    xTaskCreate(TaskEntry, "shuttle_wifi", 4096, this, 3, nullptr);
}

void ShuttleWifi::TaskEntry(void* arg) { static_cast<ShuttleWifi*>(arg)->Task(); }

void ShuttleWifi::Task() {
    const int tier_timeout = CONFIG_SHUTTLE_WIFI_TIER_TIMEOUT_S;
    const int roam_interval = CONFIG_SHUTTLE_WIFI_ROAM_INTERVAL_S;

    bool multiple;
    {
        std::lock_guard<std::recursive_mutex> lock(mutex_);
        multiple = full_.size() > 1;
    }
    if (multiple) {
        // Boot: prefer the top-priority network (normally the phone hotspot).
        TryTier(1, tier_timeout);
    } else {
        WifiManager::GetInstance().StartStation();
    }

    TickType_t last_attempt = xTaskGetTickCount();
    while (true) {
        xEventGroupWaitBits(events_, kChanged, pdTRUE, pdFALSE, pdMS_TO_TICKS(10000));
        if (roam_interval <= 0) {
            continue;
        }
        if (!(xEventGroupGetBits(events_) & kConnected)) {
            continue;  // the station keeps scanning the full list by itself
        }
        std::string ssid;
        {
            std::lock_guard<std::recursive_mutex> lock(mutex_);
            ssid = connected_ssid_;
        }
        int prio = PriorityOf(ssid);
        if (prio == 0) {
            continue;
        }
        if (xTaskGetTickCount() - last_attempt < pdMS_TO_TICKS(roam_interval * 1000)) {
            continue;
        }
        if (is_idle_ && !is_idle_()) {
            continue;
        }
        last_attempt = xTaskGetTickCount();
        ESP_LOGI(TAG, "On '%s' (priority #%d), checking for better networks", ssid.c_str(), prio + 1);
        TryTier(prio, tier_timeout);
    }
}
