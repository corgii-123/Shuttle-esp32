// Central access point for Shuttle configuration (values come from menuconfig,
// see extra_components/shuttle/Kconfig). Nothing else in the firmware should read
// CONFIG_SHUTTLE_* directly for server/Wi-Fi settings.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace shuttle {

struct WifiNetwork {
    std::string ssid;
    std::string password;
    // "" -> find phones automatically; "none" -> no ADB bridge on this network;
    // anything else -> literal host/IP of the only phone to use.
    std::string phone_host;
    std::string server_url;  // "" -> the default server URL
    int priority = 0;  // 0 = highest
};

// Networks from Kconfig, ordered by priority (index 0 = highest). Empty SSIDs skipped.
std::vector<WifiNetwork> GetConfiguredNetworks();

// The Wi-Fi in use (SSID; "" = none). Networks can have their own server URL (e.g. the Mac's
// LAN address at home); call ProvisionXiaozhiSettings() afterwards. Thread-safe.
void SetCurrentNetwork(const std::string& ssid);

// Server for the current network, e.g. "wss://shuttle.example.com" (no trailing slash).
std::string GetServerBaseUrl();
std::string GetToken();
std::string GetCtrlUrl();  // <base>/ws/ctrl
std::string GetAdbUrl();   // <base>/ws/adb
std::string GetOtaUrl();   // http(s)://<host>[/prefix]/xiaozhi/ota/

// How the phone is found on a given network:
//   kAuto     - scan the network for adbd (phone_host empty): the hotspot phone, phones on
//               the router, any number of them; the server picks one (see phone_scanner.h)
//   kFixed    - only the phone at this IP / hostname (phone_host set, no scan)
//   kDisabled - no ADB bridge on this network (phone_host == "none")
// Networks not in Kconfig (added via the Wi-Fi portal) are kAuto.
enum class PhoneHostMode { kAuto, kFixed, kDisabled };
PhoneHostMode GetPhoneHostMode(const std::string& ssid);
const char* PhoneHostModeName(PhoneHostMode mode);
std::string GetFixedPhoneHost(const std::string& ssid);  // kFixed only, else ""

// Station IPv4 info in network byte order. False if not connected.
bool GetStaIpv4(uint32_t* ip, uint32_t* netmask, uint32_t* gateway);
std::string Ipv4ToString(uint32_t addr);  // network byte order

// Phone used last on this network (NVS), tried first by the scan and used when the server
// does not answer the hello.
std::string LoadLastPhone(const std::string& ssid);
void SaveLastPhone(const std::string& ssid, const std::string& host);

std::string GetStaGatewayIp();  // "" if not connected
std::string GetStaIp();

// Writes server URL/token into the NVS namespaces that xiaozhi reads
// ("websocket": url/token/version, "wifi": ota_url). Idempotent; only writes changes.
void ProvisionXiaozhiSettings();

// Masks a secret for logging ("abcd…(12)").
std::string MaskSecret(const std::string& secret);

}  // namespace shuttle
