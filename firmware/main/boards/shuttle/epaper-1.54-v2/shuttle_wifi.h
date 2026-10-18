// Priority-ordered Wi-Fi for Shuttle, built on xiaozhi's esp-wifi-connect (WifiManager +
// SsidManager) without modifying it.
//
// esp-wifi-connect connects to the *strongest* known AP. Shuttle wants "phone hotspot
// first, home Wi-Fi as fallback", so this class:
//   1. merges Kconfig networks (priority order) with networks saved from the xiaozhi web
//      portal (kept in NVS "shuttle"/"extra_ssids" so they survive tier attempts);
//   2. at boot tries only the top-priority network for SHUTTLE_WIFI_TIER_TIMEOUT_S, then
//      falls back to the full list;
//   3. while connected to a lower-priority network and idle, retries the higher-priority ones
//      (first after SHUTTLE_WIFI_ROAM_INTERVAL_S, then doubling up to 1 h while they stay
//      absent) - e.g. hotspot -> home Wi-Fi when you get home;
//   4. RequestBestNetwork() (BOOT double-click) does that check immediately.
#pragma once

#include <freertos/FreeRTOS.h>
#include <freertos/event_groups.h>

#include <functional>
#include <mutex>
#include <string>
#include <vector>

#include <wifi_manager.h>

class ShuttleWifi {
public:
    struct Entry {
        std::string ssid;
        std::string password;
        int priority;  // 0 = highest; portal-saved networks come after Kconfig ones
    };

    static ShuttleWifi& GetInstance() {
        static ShuttleWifi instance;
        return instance;
    }

    // Build the merged list and write it to SsidManager. Call before WifiManager::StartStation().
    void Init();
    bool HasNetworks();
    // Starts the background task that runs the tiered connect / roam-up logic.
    void Start(std::function<bool()> is_idle);
    // Try higher-priority networks right now (or rescan when offline). Thread-safe.
    void RequestBestNetwork();
    // Forward every WifiManager event here (from the board's event callback).
    void OnWifiEvent(WifiEvent event, const std::string& data);
    int PriorityOf(const std::string& ssid);

private:
    ShuttleWifi();
    static void TaskEntry(void* arg);
    void Task();
    std::vector<Entry> BuildFullList();
    void LoadExtras();
    void SaveExtras();
    void MergePortalNetworks();
    void ApplyList(const std::vector<Entry>& list);
    bool TryTier(int max_priority_exclusive, int timeout_s);
    void RestartStation(const std::vector<Entry>* list);

    std::recursive_mutex mutex_;
    std::vector<Entry> extras_;
    std::vector<Entry> full_;
    EventGroupHandle_t events_ = nullptr;
    std::function<bool()> is_idle_;
    std::string connected_ssid_;
    bool task_started_ = false;
};
