"""phone_info parsers, fed with output captured from a Redmi K30 Pro (MIUI 14, Android 12)."""
from shuttle_server.adb import phone_info

BATTERY = """Current Battery Service state:
  AC powered: true
  USB powered: false
  Wireless powered: false
  Max charging current: 1700000
  Charge counter: 4591000
  status: 5
  health: 2
  present: true
  level: 100
  scale: 100
  voltage: 4436
  temperature: 282
  technology: Li-poly
"""

WIFI = """Wifi is enabled
Wifi scanning is always available
==== Primary ClientModeManager instance ====
Wifi is connected to "515"
WifiInfo: SSID: "515", BSSID: 08:60:83:27:6e:51, Security type: 2, Supplicant state: COMPLETED, \
Wi-Fi standard: 5, RSSI: -52, Link speed: 780Mbps, Tx Link speed: 780Mbps, Frequency: 5805MHz, Net ID: 1
successfulTxPackets: 238799
"""

DISKSTATS = """Latency: 1ms [512B Data Write]
Data-Free: 88095540K / 237164524K total = 37% free
System-Free: 0K / 5291404K total = 0% free
App Size: 33953594880
App Data Size: 85583973986
App Cache Size: 7332320256
Photos Size: 6935113728
Videos Size: 819363840
Audio Size: 5795840
Downloads Size: 0
System Size: 256000000000
Other Size: 35969196032
Package Names: ["com.tencent.mm","com.upgadata.up7723","com.android.settings"]
App Sizes: [400000000,150000000,0]
App Data Sizes: [17000000000,50000000,0]
Cache Sizes: [600000000,0,0]
"""

PACKAGES = "package:com.upgadata.up7723\npackage:com.tencent.mm\npackage:org.example.nosize\n"


def label(pkg):
    return {"com.tencent.mm": "微信"}.get(pkg, "")


def test_battery():
    text = phone_info.battery_text(BATTERY, "Redmi K30 Pro")
    assert "手机型号 Redmi K30 Pro" in text
    assert "电量 100%" in text and "已充满（充电器）" in text
    assert "良好" in text and "28.2°C" in text
    assert "约 4591 mAh" in text  # charge counter at full charge ~ current capacity


def test_battery_estimates_full_capacity_from_partial_charge():
    text = phone_info.battery_text("level: 50\nscale: 100\nCharge counter: 2300000\nstatus: 3\n")
    assert "电量 50%" in text and "没在充电" in text and "约 4600 mAh" in text
    assert "mAh" not in phone_info.battery_text("level: 10\nCharge counter: 460000\n")  # too inexact


def test_network():
    text = phone_info.network_text(WIFI, "中国移动,", "LTE,Unknown", "1", "3: wlan0    inet 192.168.1.6/24 brd")
    assert "Wi-Fi 已连接“515”" in text and "5 GHz" in text and "信号很好" in text and "780 Mbps" in text
    assert "IP 192.168.1.6" in text and "SIM 卡：中国移动 LTE" in text and "移动数据开着" in text
    assert phone_info.wifi_text("Wifi is disabled\n") == "Wi-Fi 关着"
    assert phone_info.wifi_text("Wifi is enabled\nWifi is not connected\n") == "Wi-Fi 开着但没有连接"


def test_storage():
    text = phone_info.storage_text(DISKSTATS, label)
    assert "存储共 242.9 GB，已用 152.6 GB，剩余 90.2 GB（已用 63%）" in text
    assert "缓存（可清理） 7.3 GB" in text and "照片 6.9 GB" in text
    assert "System" not in text and "下载" not in text  # bogus / empty categories left out
    assert "占用最多的应用：微信 18.0 GB，com.upgadata.up7723 200 MB" in text


def test_apps_sorted_by_size_with_labels():
    text = phone_info.apps_text(PACKAGES, DISKSTATS, label)
    assert text.startswith("自己安装的应用共 3 个")
    assert text.index("微信 com.tencent.mm 18.0 GB") < text.index("com.upgadata.up7723 200 MB") < \
        text.index("org.example.nosize")


def test_human_size():
    assert phone_info.human_size(999) == "999 B"
    assert phone_info.human_size(819363840) == "819 MB"
    assert phone_info.human_size(33953594880) == "34.0 GB"
