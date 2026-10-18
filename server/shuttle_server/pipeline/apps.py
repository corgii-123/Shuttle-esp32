"""App names -> Android packages, filtered by what is installed on the phone.

The model says launch_app("淘宝"); we map the spoken name to a package that `pm list packages`
reports as installed. System apps differ per brand, so a label can list several candidate
packages (first installed one wins). Unknown names fall back to a package name given directly.
"""
from __future__ import annotations

import logging
import re
import time

from ..adb.adb_cli import AdbError
from ..adb.phone import PhoneController, PhoneError

log = logging.getLogger(__name__)

# label: (aliases, candidate packages in preference order)
KNOWN_APPS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # ---- system apps (Xiaomi/Redmi first, then Huawei/Honor, OPPO/OnePlus/realme, vivo, Samsung, AOSP/Google)
    "设置": (("系统设置", "settings"), ("com.android.settings",)),
    "相机": (("照相机", "拍照", "camera"), ("com.android.camera", "com.huawei.camera", "com.oplus.camera",
                                       "com.oppo.camera", "com.sec.android.app.camera", "com.android.camera2",
                                       "com.google.android.GoogleCamera")),
    "相册": (("图库", "照片", "gallery"), ("com.miui.gallery", "com.huawei.photos", "com.coloros.gallery3d",
                                       "com.vivo.gallery", "com.sec.android.gallery3d", "com.google.android.apps.photos",
                                       "com.android.gallery3d")),
    "时钟": (("闹钟", "clock"), ("com.android.deskclock", "com.coloros.alarmclock", "com.sec.android.app.clockpackage",
                               "com.google.android.deskclock")),
    "日历": (("calendar",), ("com.android.calendar", "com.coloros.calendar", "com.samsung.android.calendar",
                           "com.google.android.calendar")),
    "天气": (("weather",), ("com.miui.weather2", "com.huawei.android.totemweather", "com.coloros.weather2",
                          "com.vivo.weather", "com.sec.android.daemonapp")),
    "计算器": (("calculator",), ("com.miui.calculator", "com.android.calculator2", "com.coloros.calculator",
                              "com.sec.android.app.popupcalculator", "com.google.android.calculator")),
    "文件管理": (("文件", "文件管理器", "files"), ("com.android.fileexplorer", "com.huawei.hidisk",
                                            "com.coloros.filemanager", "com.sec.android.app.myfiles",
                                            "com.google.android.apps.nbu.files", "com.android.documentsui")),
    "短信": (("信息", "消息", "sms"), ("com.android.mms", "com.google.android.apps.messaging",
                                   "com.samsung.android.messaging")),
    "电话": (("拨号", "打电话", "phone"), ("com.android.contacts", "com.android.dialer", "com.google.android.dialer",
                                     "com.samsung.android.dialer")),
    "联系人": (("通讯录", "contacts"), ("com.android.contacts", "com.google.android.contacts",
                                    "com.samsung.android.app.contacts")),
    "浏览器": (("browser",), ("com.android.browser", "com.huawei.browser", "com.heytap.browser", "com.vivo.browser",
                           "com.sec.android.app.sbrowser", "com.android.chrome")),
    "应用商店": (("应用市场", "商店"), ("com.xiaomi.market", "com.huawei.appmarket", "com.heytap.market",
                                  "com.oppo.market", "com.bbk.appstore", "com.android.vending")),
    "音乐": (("music",), ("com.miui.player", "com.huawei.music", "com.heytap.music", "com.android.bbkmusic")),
    "视频": (("小米视频", "华为视频"), ("com.miui.video", "com.huawei.himovie")),
    "便签": (("备忘录", "笔记", "notes"), ("com.miui.notes", "com.example.android.notepad", "com.coloros.note",
                                      "com.android.notes", "com.samsung.android.app.notes")),
    "录音机": (("录音",), ("com.android.soundrecorder", "com.huawei.soundrecorder", "com.coloros.soundrecorder")),
    "手机管家": (("安全中心", "管家"), ("com.miui.securitycenter", "com.huawei.systemmanager",
                                  "com.coloros.phonemanager", "com.iqoo.secure")),
    "指南针": ((), ("com.miui.compass",)),
    "主题": (("主题壁纸",), ("com.android.thememanager", "com.huawei.android.thememanager")),
    "小爱同学": (("小爱",), ("com.miui.voiceassist",)),
    # ---- chat / social
    "微信": (("weixin", "wechat"), ("com.tencent.mm",)),
    "QQ": (("扣扣",), ("com.tencent.mobileqq",)),
    "企业微信": ((), ("com.tencent.wework",)),
    "钉钉": ((), ("com.alibaba.android.rimet",)),
    "飞书": (("lark",), ("com.ss.android.lark",)),
    "腾讯会议": ((), ("com.tencent.wemeet.app",)),
    "微博": (("新浪微博",), ("com.sina.weibo",)),
    "小红书": ((), ("com.xingin.xhs",)),
    "知乎": ((), ("com.zhihu.android",)),
    "Telegram": (("电报",), ("org.telegram.messenger",)),
    "WhatsApp": ((), ("com.whatsapp",)),
    # ---- shopping / payment / life
    "支付宝": (("alipay",), ("com.eg.android.AlipayGphone",)),
    "淘宝": (("taobao", "手机淘宝"), ("com.taobao.taobao",)),
    "天猫": ((), ("com.tmall.wireless",)),
    "京东": ((), ("com.jingdong.app.mall",)),
    "拼多多": ((), ("com.xunmeng.pinduoduo",)),
    "闲鱼": ((), ("com.taobao.idlefish",)),
    "得物": ((), ("com.shizhuang.duapp",)),
    "唯品会": ((), ("com.achievo.vipshop",)),
    "美团": ((), ("com.sankuai.meituan",)),
    "美团外卖": ((), ("com.sankuai.meituan.takeoutnew",)),
    "饿了么": ((), ("me.ele",)),
    "大众点评": (("点评",), ("com.dianping.v1",)),
    "盒马": ((), ("com.wudaokou.hippo",)),
    "叮咚买菜": ((), ("com.yaya.zone",)),
    "菜鸟": (("菜鸟裹裹",), ("com.cainiao.wireless",)),
    "云闪付": ((), ("com.unionpay",)),
    "招商银行": ((), ("cmb.pb",)),
    "工商银行": (("中国工商银行",), ("com.icbc",)),
    "建设银行": (("中国建设银行",), ("com.chinamworld.main",)),
    "中国移动": ((), ("com.greenpoint.android.mc10086.activity",)),
    "米家": ((), ("com.xiaomi.smarthome",)),
    "小米商城": ((), ("com.xiaomi.shop",)),
    # ---- travel / maps
    "高德地图": (("高德", "地图"), ("com.autonavi.minimap",)),
    "百度地图": ((), ("com.baidu.BaiduMap",)),
    "腾讯地图": ((), ("com.tencent.map",)),
    "滴滴出行": (("滴滴", "打车"), ("com.sdu.didi.psnger",)),
    "携程": (("携程旅行",), ("ctrip.android.view",)),
    "去哪儿": (("去哪儿旅行",), ("com.Qunar",)),
    "12306": (("铁路12306", "火车票"), ("com.MobileTicket",)),
    "飞猪": ((), ("com.taobao.trip",)),
    "哈啰": (("哈啰出行",), ("com.jingyao.easybike",)),
    # ---- video / music / reading
    "抖音": (("douyin",), ("com.ss.android.ugc.aweme",)),
    "抖音极速版": ((), ("com.ss.android.ugc.aweme.lite",)),
    "快手": ((), ("com.smile.gifmaker",)),
    "快手极速版": ((), ("com.kuaishou.nebula",)),
    "哔哩哔哩": (("b站", "bilibili", "B站"), ("tv.danmaku.bili",)),
    "爱奇艺": ((), ("com.qiyi.video",)),
    "腾讯视频": ((), ("com.tencent.qqlive",)),
    "优酷": ((), ("com.youku.phone",)),
    "芒果TV": (("芒果",), ("com.hunantv.imgo.activity",)),
    "网易云音乐": (("网易云",), ("com.netease.cloudmusic",)),
    "QQ音乐": ((), ("com.tencent.qqmusic",)),
    "酷狗音乐": (("酷狗",), ("com.kugou.android",)),
    "酷我音乐": (("酷我",), ("cn.kuwo.player",)),
    "汽水音乐": ((), ("com.luna.music",)),
    "喜马拉雅": ((), ("com.ximalaya.ting.android",)),
    "今日头条": (("头条",), ("com.ss.android.article.news",)),
    "番茄小说": ((), ("com.dragon.read",)),
    "微信读书": ((), ("com.tencent.weread",)),
    "YouTube": (("油管",), ("com.google.android.youtube",)),
    # ---- tools / AI
    "百度": ((), ("com.baidu.searchbox",)),
    "夸克": (("夸克浏览器",), ("com.quark.browser",)),
    "UC浏览器": (("UC",), ("com.UCMobile",)),
    "QQ浏览器": ((), ("com.tencent.mtt",)),
    "Chrome": (("谷歌浏览器",), ("com.android.chrome",)),
    "谷歌地图": (("google地图",), ("com.google.android.apps.maps",)),
    "Gmail": ((), ("com.google.android.gm",)),
    "豆包": ((), ("com.larus.nova",)),
    "Kimi": ((), ("com.moonshot.kimichat",)),
    "DeepSeek": (("深度求索",), ("com.deepseek.chat",)),
    "通义": (("通义千问", "千问"), ("com.aliyun.tongyi",)),
    "WPS": (("wps office",), ("cn.wps.moffice_eng",)),
    "百度网盘": ((), ("com.baidu.netdisk",)),
    "QQ邮箱": ((), ("com.tencent.androidqqmail",)),
    "网易邮箱": (("网易邮箱大师",), ("com.netease.mail",)),
    "剪映": ((), ("com.lemon.lv",)),
    "美图秀秀": ((), ("com.mt.mtxx.mtxx",)),
    "Keep": ((), ("com.gotokeep.keep",)),
}

PACKAGE_RE = re.compile(r"^[A-Za-z][\w]*(\.[A-Za-z_][\w]*)+$")
CACHE_S = 300.0


def _norm(name: str) -> str:
    return re.sub(r"[\s·・\-_]", "", name).lower().removesuffix("app")


class AppCatalog:
    def __init__(self) -> None:
        self._installed: dict[str, tuple[float, set[str]]] = {}
        self._by_package: dict[str, str] = {}
        for label, (_, pkgs) in KNOWN_APPS.items():
            for p in pkgs:
                self._by_package.setdefault(p, label)

    def label(self, package: str) -> str:
        return self._by_package.get(package, "")

    async def installed(self, phone: PhoneController, refresh: bool = False) -> set[str]:
        serial = phone.serial
        cached = self._installed.get(serial)
        if cached and not refresh and time.monotonic() - cached[0] < CACHE_S:
            return cached[1]
        try:
            pkgs = await phone.installed_packages()
        except (PhoneError, AdbError) as e:
            log.warning("pm list packages failed: %s", e)
            return cached[1] if cached else set()
        self._installed[serial] = (time.monotonic(), pkgs)
        return pkgs

    async def installed_labels(self, phone: PhoneController) -> list[str]:
        """Known app labels that are installed (for the system prompt)."""
        pkgs = await self.installed(phone)
        return [label for label, (_, cands) in KNOWN_APPS.items() if any(p in pkgs for p in cands)]

    def _candidates(self, name: str, exact_only: bool = False) -> list[str]:
        n = _norm(name)
        exact, partial = [], []
        for label, (aliases, pkgs) in KNOWN_APPS.items():
            keys = [_norm(label), *(_norm(a) for a in aliases)]
            if n in keys:
                exact.extend(pkgs)
            elif any(k and (k in n or n in k) for k in keys) and len(n) >= 2:
                partial.extend(pkgs)
        return exact if exact_only else exact + partial

    async def resolve(self, phone: PhoneController, name: str, exact: bool = False) -> str | None:
        """Installed package for a spoken app name (or a package name). exact=True: only a full
        label/alias match, no substring guessing (used by the no-LLM fast path)."""
        name = name.strip()
        if not name:
            return None
        for refresh in (False, True):
            pkgs = await self.installed(phone, refresh=refresh)
            if PACKAGE_RE.match(name) and (name in pkgs or not pkgs):
                return name
            for p in self._candidates(name, exact_only=exact):
                if p in pkgs:
                    return p
        return None
