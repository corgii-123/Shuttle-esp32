// Central access point for Shuttle configuration (values come from menuconfig,
// see extra_components/shuttle/Kconfig). Nothing else in the firmware should read
// CONFIG_SHUTTLE_* directly for server/Wi-Fi settings.
#pragma once

#include <string>
#include <vector>

namespace shuttle {

struct WifiNetwork {
    std::string ssid;
    std::string password;
    // "" -> use the DHCP gateway (phone hotspot); "none" -> no ADB bridge on this
    // network; anything else -> literal host/IP of the phone.
    std::string phone_host;
    int priority = 0;  // 0 = highest
};

// Networks from Kconfig, ordered by priority (index 0 = highest). Empty SSIDs skipped.
std::vector<WifiNetwork> GetConfiguredNetworks();

std::string GetServerBaseUrl();  // e.g. "wss://shuttle.example.com" (no trailing slash)
std::string GetToken();
std::string GetCtrlUrl();  // <base>/ws/ctrl
std::string GetAdbUrl();   // <base>/ws/adb
std::string GetOtaUrl();   // http(s)://<host>[/prefix]/xiaozhi/ota/

// How the phone is reached on a given network:
//   kGateway  - phone hotspot: the phone is the DHCP gateway (phone_host empty)
//   kFixed    - router / home Wi-Fi: phone at a fixed IP or hostname (phone_host set)
//   kDisabled - no ADB bridge on this network (phone_host == "none")
// Networks not in Kconfig (added via the Wi-Fi portal) are treated as kGateway.
enum class PhoneHostMode { kGateway, kFixed, kDisabled };
PhoneHostMode GetPhoneHostMode(const std::string& ssid);
const char* PhoneHostModeName(PhoneHostMode mode);

// Phone address for the network with this SSID. Returns "" when the bridge should not
// run (kDisabled, or kGateway without an IP yet).
std::string ResolvePhoneHost(const std::string& ssid);

std::string GetStaGatewayIp();  // "" if not connected
std::string GetStaIp();

// Writes server URL/token into the NVS namespaces that xiaozhi reads
// ("websocket": url/token/version, "wifi": ota_url). Idempotent; only writes changes.
void ProvisionXiaozhiSettings();

// Masks a secret for logging ("abcd…(12)").
std::string MaskSecret(const std::string& secret);

}  // namespace shuttle
