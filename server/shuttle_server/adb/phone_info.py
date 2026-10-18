"""Phone status straight from adb, without navigating the screen: battery, network, storage, apps.

The agent's phone_info tool uses this for questions like "电量多少" / "连的什么网" /
"存储够不够" / "装了哪些游戏": a couple of shell commands answer them in about a second, where
finding the right Settings page took the vision model a minute (or failed). The result is short
Chinese text for the model. Parsing is tolerant: what a ROM does not report is left out.
"""
from __future__ import annotations

import json
import re
from typing import Callable

from .phone import PhoneController

TOPICS = {"battery": "电池信息", "network": "网络信息", "storage": "存储空间", "apps": "已装应用"}

_SEP = "@@SHUTTLE@@"
_STATUS = {"2": "正在充电", "3": "没在充电", "4": "接着电源但没在充电", "5": "已充满"}
_HEALTH = {"2": "良好", "3": "过热", "4": "已损坏", "5": "电压过高", "6": "故障", "7": "温度过低"}
_SIZE_FIELDS = (("App Size", "应用"), ("App Data Size", "应用数据"), ("App Cache Size", "缓存（可清理）"),
                ("Photos Size", "照片"), ("Videos Size", "视频"), ("Audio Size", "音频"),
                ("Downloads Size", "下载"), ("Other Size", "其他"))
MAX_APPS = 80


def human_size(n: float) -> str:
    """Decimal units, as Android's own storage pages show them."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000:
            return f"{n:.0f} {unit}" if unit != "GB" else f"{n:.1f} GB"
        n /= 1000
    return f"{n:.1f} TB"


def _fields(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition(":")
        if sep and key.strip() and key.strip() not in out:
            out[key.strip()] = value.strip()
    return out


def _int(value: str | None) -> int | None:
    return int(value) if value and re.fullmatch(r"-?\d+", value) else None


def battery_text(dumpsys_battery: str, model: str = "") -> str:
    f = _fields(dumpsys_battery)
    parts = [f"手机型号 {model}"] if model else []
    level, scale = _int(f.get("level")), _int(f.get("scale")) or 100
    pct = round(level * 100 / scale) if level is not None else None
    if pct is not None:
        parts.append(f"电量 {pct}%")
    sources = [name for key, name in (("AC powered", "充电器"), ("USB powered", "USB"), ("Wireless powered", "无线"))
               if f.get(key) == "true"]
    status = _STATUS.get(f.get("status", ""))
    if status:
        parts.append(status + (f"（{'、'.join(sources)}）" if sources else ""))
    health = _HEALTH.get(f.get("health", ""))
    if health:
        parts.append(f"系统报告的电池健康：{health}（安卓只给良好/过热/损坏这类粗略状态）")
    temp = _int(f.get("temperature"))
    if temp is not None:
        parts.append(f"电池温度 {temp / 10:.1f}°C")
    counter = _int(f.get("Charge counter"))  # µAh left in the battery
    if counter and pct and pct >= 20:
        full = counter * 100 // pct // 1000
        parts.append(f"按电量计估算，充满时约 {full} mAh（和这款手机的标称容量比，可估算电池损耗）")
    return "；".join(parts) + "。" if parts else "读不到电池信息。"


def wifi_text(status: str) -> str:
    if "Wifi is disabled" in status:
        return "Wi-Fi 关着"
    m = re.search(r'Wifi is connected to "(.*?)"', status) or re.search(r'SSID: "(.*?)"', status)
    if not m or m.group(1) in ("", "<unknown ssid>"):
        return "Wi-Fi 开着但没有连接"
    info = [f"Wi-Fi 已连接“{m.group(1)}”"]
    freq = re.search(r"Frequency: (\d+)\s*MHz", status)
    if freq:
        mhz = int(freq.group(1))
        info.append("6 GHz" if mhz >= 5925 else "5 GHz" if mhz >= 4900 else "2.4 GHz")
    rssi = re.search(r"RSSI: (-?\d+)", status)
    if rssi:
        dbm = int(rssi.group(1))
        quality = "很好" if dbm >= -55 else "良好" if dbm >= -67 else "一般" if dbm >= -75 else "较弱"
        info.append(f"信号{quality}（{dbm} dBm）")
    speed = re.search(r"Link speed: (\d+)\s*Mbps", status)
    if speed:
        info.append(f"连接速率 {speed.group(1)} Mbps")
    return info[0] + ("（" + "，".join(info[1:]) + "）" if len(info) > 1 else "")


def network_text(wifi_status: str, operators: str, net_types: str, mobile_data: str, ip_addr: str) -> str:
    parts = [wifi_text(wifi_status)]
    ip = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", ip_addr)
    if ip and "已连接" in parts[0]:
        parts.append(f"手机 IP {ip.group(1)}")
    sims = [s.strip() for s in operators.split(",")]
    types = [t.strip() for t in net_types.split(",")]
    cards = []
    for i, name in enumerate(sims):
        if not name:
            continue
        kind = types[i] if i < len(types) and types[i] not in ("", "Unknown") else ""
        cards.append(name + (f" {kind}" if kind else ""))
    if cards:
        parts.append("SIM 卡：" + "、".join(cards))
    if mobile_data.strip() in ("0", "1"):
        parts.append("移动数据" + ("开着" if mobile_data.strip() == "1" else "关着"))
    return "；".join(parts) + "。"


def _diskstats_packages(diskstats: str) -> dict[str, int]:
    """package -> app + data + cache bytes (system cache of the last storage scan)."""
    lists: dict[str, list] = {}
    for key in ("Package Names", "App Sizes", "App Data Sizes", "Cache Sizes"):
        m = re.search(rf"^{key}: (\[.*\])\s*$", diskstats, re.M)
        if m:
            try:
                lists[key] = json.loads(m.group(1))
            except ValueError:
                pass
    names = lists.get("Package Names") or []
    sizes: dict[str, int] = {}
    for i, name in enumerate(names):
        total = 0
        for key in ("App Sizes", "App Data Sizes", "Cache Sizes"):
            values = lists.get(key) or []
            if i < len(values) and isinstance(values[i], (int, float)):
                total += int(values[i])
        sizes[str(name)] = total
    return sizes


def storage_text(diskstats: str, label: Callable[[str], str] = lambda p: "") -> str:
    parts = []
    m = re.search(r"Data-Free: (\d+)K / (\d+)K total", diskstats)
    if m:
        free, total = int(m.group(1)) * 1024, int(m.group(2)) * 1024
        parts.append(f"存储共 {human_size(total)}，已用 {human_size(total - free)}，剩余 {human_size(free)}"
                     f"（已用 {round((total - free) * 100 / total)}%）")
    f = _fields(diskstats)
    cats = [f"{name} {human_size(v)}" for key, name in _SIZE_FIELDS if (v := _int(f.get(key))) and v > 0]
    if cats:
        parts.append("分类（系统定期统计，可能不是最新）：" + "，".join(cats))
    top = sorted(_diskstats_packages(diskstats).items(), key=lambda kv: -kv[1])[:8]
    if top:
        parts.append("占用最多的应用：" + "，".join(f"{label(p) or p} {human_size(s)}" for p, s in top if s > 0))
    return "；".join(parts) + "。" if parts else "读不到存储信息。"


def apps_text(packages: str, diskstats: str, label: Callable[[str], str] = lambda p: "") -> str:
    pkgs = sorted({line.split(":", 1)[1].strip() for line in packages.splitlines() if line.startswith("package:")})
    if not pkgs:
        return "读不到已安装的应用。"
    sizes = _diskstats_packages(diskstats)
    pkgs.sort(key=lambda p: -sizes.get(p, -1))
    shown = []
    for p in pkgs[:MAX_APPS]:
        name = label(p)
        size = sizes.get(p, 0)
        shown.append((f"{name} {p}" if name else p) + (f" {human_size(size)}" if size > 0 else ""))
    more = f"；另有 {len(pkgs) - MAX_APPS} 个没列出" if len(pkgs) > MAX_APPS else ""
    return (f"自己安装的应用共 {len(pkgs)} 个（按占用空间从大到小；多数只有包名，"
            f"请按包名判断是什么应用、是不是游戏）：" + "；".join(shown) + more + "。")


async def _run(phone: PhoneController, *commands: str) -> list[str]:
    """Several commands in one adb round trip; failures of single commands give ''."""
    script = f"; echo {_SEP}; ".join(f"({c}) 2>/dev/null" for c in commands)
    out = await phone.shell(script, check=False)
    parts = out.split(_SEP)
    return [p.strip() for p in parts] + [""] * (len(commands) - len(parts))


async def read(phone: PhoneController, topic: str, label: Callable[[str], str] = lambda p: "") -> str:
    if topic == "battery":
        battery, model = await _run(phone, "dumpsys battery",
                                    "getprop ro.product.marketname || getprop ro.product.model")
        return battery_text(battery, model.splitlines()[0] if model else "")
    if topic == "network":
        wifi, ops, types, data, ip = await _run(phone, "cmd wifi status", "getprop gsm.operator.alpha",
                                                "getprop gsm.network.type", "settings get global mobile_data",
                                                "ip -4 -o addr show wlan0")
        if "Wifi is" not in wifi:  # no `cmd wifi` (older Android)
            wifi = (await _run(phone, "dumpsys wifi | grep -m 2 -E 'mWifiInfo|Wi-Fi is'"))[0]
        return network_text(wifi, ops, types, data, ip)
    if topic == "storage":
        (stats,) = await _run(phone, "dumpsys diskstats")
        return storage_text(stats, label)
    if topic == "apps":
        pkgs, stats = await _run(phone, "pm list packages -3", "dumpsys diskstats")
        return apps_text(pkgs, stats, label)
    raise ValueError(f"topic 只能是 {'/'.join(TOPICS)}，收到 {topic}")
