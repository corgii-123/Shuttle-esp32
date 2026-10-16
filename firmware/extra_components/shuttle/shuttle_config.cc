#include "shuttle/shuttle_config.h"

#include <esp_log.h>
#include <esp_netif.h>
#include <nvs.h>
#include <sdkconfig.h>

#include <cstring>

#define TAG "ShuttleConfig"

namespace shuttle {

namespace {

std::string TrimTrailingSlash(std::string s) {
    while (!s.empty() && s.back() == '/') {
        s.pop_back();
    }
    return s;
}

// "wss://host/prefix" -> "https://host/prefix", "ws://..." -> "http://..."
std::string ToHttpScheme(const std::string& url) {
    if (url.rfind("wss://", 0) == 0) {
        return "https://" + url.substr(6);
    }
    if (url.rfind("ws://", 0) == 0) {
        return "http://" + url.substr(5);
    }
    return url;
}

bool NvsWriteStringIfChanged(nvs_handle_t handle, const char* key, const std::string& value) {
    char buf[256];
    size_t len = sizeof(buf);
    if (nvs_get_str(handle, key, buf, &len) == ESP_OK && value == buf) {
        return false;
    }
    esp_err_t err = nvs_set_str(handle, key, value.c_str());
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "nvs_set_str(%s) failed: %s", key, esp_err_to_name(err));
        return false;
    }
    return true;
}

void ProvisionNamespace(const char* ns, const char* key, const std::string& value) {
    nvs_handle_t handle;
    esp_err_t err = nvs_open(ns, NVS_READWRITE, &handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "nvs_open(%s) failed: %s", ns, esp_err_to_name(err));
        return;
    }
    if (NvsWriteStringIfChanged(handle, key, value)) {
        nvs_commit(handle);
        ESP_LOGI(TAG, "Provisioned %s.%s", ns, key);
    }
    nvs_close(handle);
}

esp_netif_ip_info_t GetStaIpInfo(bool* ok) {
    esp_netif_ip_info_t info = {};
    *ok = false;
    esp_netif_t* netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    if (netif != nullptr && esp_netif_get_ip_info(netif, &info) == ESP_OK && info.ip.addr != 0) {
        *ok = true;
    }
    return info;
}

}  // namespace

std::vector<WifiNetwork> GetConfiguredNetworks() {
    std::vector<WifiNetwork> list;
    auto add = [&list](const char* ssid, const char* password, const char* phone_host) {
        if (ssid == nullptr || ssid[0] == '\0') {
            return;
        }
        WifiNetwork n;
        n.ssid = ssid;
        n.password = password ? password : "";
        n.phone_host = phone_host ? phone_host : "";
        n.priority = static_cast<int>(list.size());
        list.push_back(std::move(n));
    };
    add(CONFIG_SHUTTLE_WIFI1_SSID, CONFIG_SHUTTLE_WIFI1_PASSWORD, CONFIG_SHUTTLE_WIFI1_PHONE_HOST);
    add(CONFIG_SHUTTLE_WIFI2_SSID, CONFIG_SHUTTLE_WIFI2_PASSWORD, CONFIG_SHUTTLE_WIFI2_PHONE_HOST);
    add(CONFIG_SHUTTLE_WIFI3_SSID, CONFIG_SHUTTLE_WIFI3_PASSWORD, CONFIG_SHUTTLE_WIFI3_PHONE_HOST);
    return list;
}

std::string GetServerBaseUrl() { return TrimTrailingSlash(CONFIG_SHUTTLE_SERVER_URL); }

std::string GetToken() { return CONFIG_SHUTTLE_TOKEN; }

std::string GetCtrlUrl() { return GetServerBaseUrl() + "/ws/ctrl"; }

std::string GetAdbUrl() { return GetServerBaseUrl() + "/ws/adb"; }

std::string GetOtaUrl() { return ToHttpScheme(GetServerBaseUrl()) + "/xiaozhi/ota/"; }

std::string GetStaGatewayIp() {
    bool ok = false;
    auto info = GetStaIpInfo(&ok);
    if (!ok || info.gw.addr == 0) {
        return "";
    }
    char buf[16];
    esp_ip4addr_ntoa(&info.gw, buf, sizeof(buf));
    return buf;
}

std::string GetStaIp() {
    bool ok = false;
    auto info = GetStaIpInfo(&ok);
    if (!ok) {
        return "";
    }
    char buf[16];
    esp_ip4addr_ntoa(&info.ip, buf, sizeof(buf));
    return buf;
}

PhoneHostMode GetPhoneHostMode(const std::string& ssid) {
    for (const auto& n : GetConfiguredNetworks()) {
        if (n.ssid != ssid) {
            continue;
        }
        if (n.phone_host == "none") {
            return PhoneHostMode::kDisabled;
        }
        return n.phone_host.empty() ? PhoneHostMode::kAuto : PhoneHostMode::kFixed;
    }
    return PhoneHostMode::kAuto;
}

const char* PhoneHostModeName(PhoneHostMode mode) {
    switch (mode) {
        case PhoneHostMode::kAuto: return "auto(scan)";
        case PhoneHostMode::kFixed: return "fixed";
        case PhoneHostMode::kDisabled: return "disabled";
    }
    return "?";
}

std::string GetFixedPhoneHost(const std::string& ssid) {
    for (const auto& n : GetConfiguredNetworks()) {
        if (n.ssid == ssid && n.phone_host != "none") {
            return n.phone_host;
        }
    }
    return "";
}

bool GetStaIpv4(uint32_t* ip, uint32_t* netmask, uint32_t* gateway) {
    bool ok = false;
    auto info = GetStaIpInfo(&ok);
    if (!ok) {
        return false;
    }
    *ip = info.ip.addr;
    *netmask = info.netmask.addr;
    *gateway = info.gw.addr;
    return true;
}

std::string Ipv4ToString(uint32_t addr) {
    esp_ip4_addr_t a = {.addr = addr};
    char buf[16];
    esp_ip4addr_ntoa(&a, buf, sizeof(buf));
    return buf;
}

std::string LoadLastPhone(const std::string& ssid) {
    nvs_handle_t handle;
    if (nvs_open("shuttle", NVS_READONLY, &handle) != ESP_OK) {
        return "";
    }
    char buf[128];
    size_t len = sizeof(buf);
    std::string value;
    if (nvs_get_str(handle, "last_phone", buf, &len) == ESP_OK) {
        value = buf;
    }
    nvs_close(handle);
    // stored as "<ssid>\t<host>"
    auto tab = value.rfind('\t');
    if (tab == std::string::npos || value.substr(0, tab) != ssid) {
        return "";
    }
    return value.substr(tab + 1);
}

void SaveLastPhone(const std::string& ssid, const std::string& host) {
    if (ssid.size() + host.size() + 2 > 128 || LoadLastPhone(ssid) == host) {
        return;
    }
    nvs_handle_t handle;
    if (nvs_open("shuttle", NVS_READWRITE, &handle) != ESP_OK) {
        return;
    }
    if (NvsWriteStringIfChanged(handle, "last_phone", ssid + "\t" + host)) {
        nvs_commit(handle);
        ESP_LOGI(TAG, "Last phone on '%s' = %s", ssid.c_str(), host.c_str());
    }
    nvs_close(handle);
}

void ProvisionXiaozhiSettings() {
    ESP_LOGI(TAG, "Server: %s  token: %s", GetServerBaseUrl().c_str(), MaskSecret(GetToken()).c_str());
    ProvisionNamespace("websocket", "url", GetCtrlUrl());
    ProvisionNamespace("websocket", "token", GetToken());
    ProvisionNamespace("wifi", "ota_url", GetOtaUrl());
}

std::string MaskSecret(const std::string& secret) {
    if (secret.size() <= 4) {
        return "****";
    }
    return secret.substr(0, 4) + "...(" + std::to_string(secret.size()) + ")";
}

}  // namespace shuttle
