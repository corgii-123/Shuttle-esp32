"""LlmAgent: a vision LLM drives the phone (screenshot -> tool call -> adb) until it calls finish().

Talks to any OpenAI-compatible /chat/completions endpoint with tool calling, e.g. the local
Splash server running Qwen3.8-27B (vision) on :8000. Per step the model gets the task, a short
history of what it already did, the current app and the latest screenshot (older screenshots
are dropped to keep the prompt small) and answers with exactly one tool call.

Coordinates: Qwen-VL models answer in a 0..1000 grid over the image (agent.llm.coords=norm1000);
they are converted to screenshot pixels here and to device pixels by PhoneController.

Without vision (agent.llm.vision=false, or a text-only model detected via GET /models) the
model gets the uiautomator element list (text, centre point) instead of the image.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from ..adb import phone_info
from ..adb.phone import PhoneController, PhoneError, PhoneUnavailable, Screenshot, UiElement
from ..adb.adb_cli import AdbError
from ..config import LlmAgentConfig
from .agent import Agent, AgentContext
from .apps import AppCatalog

log = logging.getLogger(__name__)

TOOL_ERRORS = (PhoneError, AdbError, KeyError, ValueError, TypeError)
TOOL_LABELS = {"tap": "点击", "long_press": "长按", "swipe": "滑动", "scroll": "翻页", "type_text": "输入",
               "launch_app": "打开应用", "back": "返回", "home": "回桌面", "wait": "等待",
               "phone_info": "读取手机状态"}
MAX_CONSECUTIVE_FAILURES = 3  # stop instead of burning all steps on the same broken action
# Paging budget. The prompt asks for at most 3 scrolls, but a lost model pages up and down until
# the step limit (11 scrolls in a row in Settings, with the wanted item on screen).
MAX_SCROLLS_IN_A_ROW = 4
MAX_SCROLLS = 8  # per task


def _fn(name: str, description: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": props, "required": required}}}


def _point(desc: str) -> dict[str, Any]:
    # [x, y] like Qwen-VL's grounding output ("point_2d"); separate x/y integers made Qwen3.8
    # put the pair into "x" (x=[347, 531]).
    return {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2, "description": desc}


_TARGET = {"target": {"type": "string", "description": "点的是什么（显示给用户，如“淘宝图标”）"}}
# Only the newest screenshot is kept: the model writes down what it still needs from this page.
_NOTE = {"note": {"type": "string", "description": "可选：这一页上和任务有关、之后还要用的信息（旧截图不会保留）"}}

TOOLS = [
    _fn("tap", "点击屏幕上的一个位置。", {"point_2d": _point("[x, y]"), **_TARGET, **_NOTE}, ["point_2d"]),
    _fn("long_press", "长按屏幕上的一个位置。", {"point_2d": _point("[x, y]"), **_TARGET, **_NOTE}, ["point_2d"]),
    _fn("swipe", "从起点滑动到终点（手指移动的方向）。", {
        "start_2d": _point("起点 [x, y]"), "end_2d": _point("终点 [x, y]"), **_NOTE}, ["start_2d", "end_2d"]),
    _fn("scroll", "滚动页面查看更多内容。down=查看下面的内容，up=查看上面的内容，left/right=左右翻页。", {
        "direction": {"type": "string", "enum": ["down", "up", "left", "right"]}, **_NOTE}, ["direction"]),
    _fn("phone_info", "直接读取手机自身的状态，不用去设置里找：battery=电量、充电、电池温度/健康/容量，"
        "network=连的 Wi-Fi、信号、IP、SIM 卡/移动数据，storage=存储空间和占用最多的应用，"
        "apps=自己安装的所有应用（含大小）。", {
            "topic": {"type": "string", "enum": list(phone_info.TOPICS)}}, ["topic"]),
    _fn("type_text", "在当前已获得焦点的输入框中输入文字（输入前先点击输入框）。submit=true 时输入后按回车/搜索。", {
        "text": {"type": "string"}, "submit": {"type": "boolean"}}, ["text"]),
    _fn("launch_app", "直接打开一个应用（比在桌面找图标快）。app 为应用名或包名。", {
        "app": {"type": "string"}, **_NOTE}, ["app"]),
    _fn("back", "按返回键。", {**_NOTE}, []),
    _fn("home", "回到桌面。", {}, []),
    _fn("wait", "等待页面加载。", {"seconds": {"type": "number", "description": "1-5 秒"}}, []),
    _fn("finish", "任务完成、无法完成或需要用户决定时调用，结束任务。reply 会被语音播报给用户。", {
        "reply": {"type": "string", "description": "念给用户听的简短中文回复：一两句话，不超过 40 字；内容多时只说最重要的 1～3 条"},
        "success": {"type": "boolean"}}, ["reply"]),
]

SYSTEM_PROMPT = """你是“搞快”，一个通过语音帮用户操作安卓手机的助手。用户按住设备说一句话，你看着手机屏幕一步步操作，直到完成，再用一两句话告诉用户结果。

怎么操作：
- 每一步只调用一个工具。先看清当前屏幕再决定；动作之后会给你新截图，用它确认上一步的效果。
- {coords}
- 打开应用用 launch_app，不要去桌面找图标。
- 问手机自身的状态（电量/电池、网络/Wi-Fi、存储空间、装了哪些应用）时，先用 phone_info 直接读取，读到的就是答案，不用去设置里找。
- 找东西（联系人、聊天、商品、设置项、文件……）优先用页面里的搜索（设置页顶部也有搜索框）；连续翻页最多 3 次还没找到，就改用搜索或换个入口。
- 只保留最新一张截图：翻页或离开页面前，把这一页上还要用的信息写进 note。
- 输入文字前先点中输入框；需要提交时用 type_text 的 submit，或点页面上的发送/搜索/确定按钮。输入的内容要忠实于用户的话。
- 页面在加载就 wait。弹窗、广告、引导页、权限请求：不影响任务的先关掉或跳过（权限选“仅在使用中允许”）。
- 操作后屏幕没变化，说明点偏了或没生效：换个位置或换个方法，不要重复同一个动作。走错了就 back，必要时 home 回桌面重来。
- 做完先在屏幕上确认结果（确实已打开、已发送、开关已切换……），再调用 finish。
- 用户要的是信息（天气、余额、消息内容……）时，把屏幕上看到的答案用 finish 说出来。
- 用户的话来自语音识别，可能有同音错字，按最合理的意思理解。

安全：
- 付款、转账、下单、删除、卸载、退出登录、修改密码或隐私设置这类不可撤销的操作，只在用户明确要求时做；确认弹窗里如实确认。
- 遇到锁屏密码、登录、验证码、支付密码、人脸识别时停下，用 finish 告诉用户。
- 截图全黑多半是无法截屏的安全页面（支付等），用 finish 说明。

finish 的 reply 会直接念给用户听：口语化，一两句话，不超过 40 个字。内容多（列表、热榜、新闻、消息）时只挑最重要的 1～3 条，每条几个字概括，不要照着屏幕逐条念。

{apps}"""

# A bare "open <app>" needs no screen reading: "打开设置" / "帮我打开微信。" / "启动 抖音"
FAST_LAUNCH = re.compile(r"^(?:请|麻烦)?(?:帮我|给我)?(?:打开一下|开一下|打开|启动)\s*(?P<app>[^，。,.!！?？\s]{1,12}?)(?:一下)?[。.!！]?$")

COORDS_NORM = "坐标写成 [x, y]，使用 0 到 1000 的归一化坐标：屏幕左上角是 [0, 0]，右下角是 [1000, 1000]，与截图的实际像素无关。"
COORDS_PIXEL = "坐标写成 [x, y]，使用截图像素坐标：左上角是 [0, 0]，截图尺寸见每一步的说明。"


@dataclass
class _Turn:
    instruction: str
    reply: str


@dataclass
class _ModelInfo:
    model: str
    vision: bool
    checked_at: float = field(default_factory=time.monotonic)


class LlmError(RuntimeError):
    pass


def _strip_code_fence(text: str) -> str:
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    return m.group(1).strip() if m else text.strip()


def parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    text = _strip_code_fence(str(raw))
    try:
        val = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise
        val = json.loads(m.group(0))
    return val if isinstance(val, dict) else {}


def to_number(value: Any) -> float:
    """Coordinates as models really send them: 347, "347", [347], "[347]", " 347px"."""
    if isinstance(value, bool):
        raise ValueError(f"坐标不能是 {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (list, tuple)) and value:
        return to_number(value[0])
    if isinstance(value, str):
        m = re.search(r"-?\d+(?:\.\d+)?", value)
        if m:
            return float(m.group(0))
    raise ValueError(f"坐标格式不对: {value!r}（应为整数）")


def point(args: dict[str, Any], key: str = "point_2d", xk: str = "x", yk: str = "y") -> tuple[float, float]:
    """A point from tool arguments, in every shape models produce: point_2d=[x, y] (the schema),
    x/y integers, x=[x] y=[y], or the pair stuffed into x (x=[x, y], y=[y, y] - seen from Qwen3.8)."""
    def pair(v: Any) -> tuple[Any, Any] | None:
        if isinstance(v, (list, tuple)) and len(v) >= 2:
            return v[0], v[1]
        if isinstance(v, str) and len(re.findall(r"-?\d+(?:\.\d+)?", v)) >= 2:
            a, b = re.findall(r"-?\d+(?:\.\d+)?", v)[:2]
            return a, b
        return None

    for v in (args.get(key), args.get(xk)):
        p = pair(v)
        if p is not None:
            return to_number(p[0]), to_number(p[1])
    x, y = args.get(xk), args.get(yk)
    if x is None or y is None:
        raise ValueError(f"缺少坐标（{key} 应为 [x, y]）")
    return to_number(x), to_number(y)


def extract_tool_call(message: dict[str, Any]) -> tuple[str, dict[str, Any], str] | None:
    """(name, arguments, call_id) from an assistant message.

    Accepts proper `tool_calls`, and the fallbacks local servers sometimes produce: Qwen's raw
    `<tool_call>{...}</tool_call>` or a bare JSON object in `content`.
    """
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        if fn.get("name"):
            try:
                args = parse_arguments(fn.get("arguments"))
            except json.JSONDecodeError:
                args = {}
            return fn["name"], args, call.get("id") or "call_0"
    content = message.get("content") or ""
    if not isinstance(content, str):
        return None
    m = re.search(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|$)", content, re.S)
    candidate = m.group(1) if m else _strip_code_fence(content)
    if candidate.startswith("{"):
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            return None
        name = obj.get("name") or obj.get("action") or obj.get("tool")
        if isinstance(name, str):
            args = obj.get("arguments", obj.get("parameters", obj.get("args", {})))
            try:
                return name, parse_arguments(args), "call_0"
            except json.JSONDecodeError:
                return name, {}, "call_0"
    return None


class LlmAgent(Agent):
    name = "llm"

    def __init__(self, cfg: LlmAgentConfig):
        self.cfg = cfg
        self.apps = AppCatalog()
        self._session: aiohttp.ClientSession | None = None
        self._model: _ModelInfo | None = None
        self._memory: dict[str, list[_Turn]] = {}

    # ------------------------------------------------------------------ LLM plumbing
    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.cfg.timeout_s))
        return self._session

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.cfg.api_key}"} if self.cfg.api_key else {}

    async def model_info(self) -> _ModelInfo:
        """Resolve the model id and whether it accepts images (cached for 10 min)."""
        if self._model is not None and time.monotonic() - self._model.checked_at < 600:
            return self._model
        base = self.cfg.base_url.rstrip("/")
        entries: list[dict[str, Any]] = []
        try:
            async with self._http().get(f"{base}/models", headers=self._headers(),
                                        timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    entries = (await r.json(content_type=None)).get("data") or []
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            log.warning("LLM %s/models not reachable: %s", base, e)
        entry = next((e for e in entries if e.get("id") == self.cfg.model), None)
        if entry is None and not self.cfg.model and entries:
            entry = entries[0]
        model = self.cfg.model or (entry or {}).get("id", "")
        if not model:
            raise LlmError(f"no model configured and none listed at {base}/models")
        reported = None
        if entry is not None:
            if isinstance(entry.get("vision"), bool):
                reported = entry["vision"]
            elif isinstance(entry.get("input_modalities"), list):
                reported = "image" in entry["input_modalities"]
        if self.cfg.vision == "auto":
            vision = True if reported is None else reported
        else:
            vision = self.cfg.vision == "true"
            if reported is False and vision:
                log.error("agent.llm.model %s reports no image input, but agent.llm.vision=true", model)
        if not vision:
            log.warning("LLM %s has no vision: using the UI tree (uiautomator) instead of screenshots", model)
        self._model = _ModelInfo(model, vision)
        log.info("LLM agent model=%s vision=%s (server says %s)", model, vision, reported)
        return self._model

    async def chat(self, messages: list[dict[str, Any]], model: str) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": model, "messages": messages, "tools": TOOLS, "tool_choice": "auto",
                                   "temperature": self.cfg.temperature, "max_tokens": self.cfg.max_tokens}
        if self.cfg.reasoning_effort:
            payload["reasoning_effort"] = self.cfg.reasoning_effort
        base = self.cfg.base_url.rstrip("/")
        t0 = time.monotonic()
        async with self._http().post(f"{base}/chat/completions", json=payload, headers=self._headers()) as r:
            body = await r.text()
            if r.status != 200:
                raise LlmError(f"LLM HTTP {r.status}: {body[:300]}")
        data = json.loads(body)
        usage = data.get("usage") or {}
        log.info("LLM %d ms, prompt %s tok, completion %s tok", (time.monotonic() - t0) * 1000,
                 usage.get("prompt_tokens"), usage.get("completion_tokens"))
        return data["choices"][0]["message"]

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------ prompt building
    def _system_prompt(self, installed: list[str]) -> str:
        apps = ("可以用 launch_app 直接打开的应用：" + "、".join(installed) + "。其他应用也可以用包名打开。\n") \
            if installed else ""
        # Keep the system prompt stable (no clock etc.): the server can then reuse its prompt cache.
        prompt = SYSTEM_PROMPT.format(coords=COORDS_NORM if self.cfg.coords == "norm1000" else COORDS_PIXEL,
                                      apps=apps)
        if self.cfg.extra_prompt:
            prompt += "\n" + self.cfg.extra_prompt
        return prompt

    def _to_model(self, x: float, y: float, shot: Screenshot | None) -> tuple[int, int]:
        if self.cfg.coords == "norm1000" and shot is not None and shot.width and shot.height:
            return round(x * 1000 / shot.width), round(y * 1000 / shot.height)
        return round(x), round(y)

    def _from_model(self, x: float, y: float, shot: Screenshot | None) -> tuple[float, float]:
        if self.cfg.coords == "norm1000" and shot is not None:
            x = min(max(float(x), 0.0), 1000.0)
            y = min(max(float(y), 0.0), 1000.0)
            return x * shot.width / 1000, y * shot.height / 1000
        return float(x), float(y)

    def _describe_elements(self, elems: list[UiElement], shot: Screenshot | None, limit: int = 60) -> str:
        lines = []
        for e in elems:
            label = (e.label or "").replace("\n", " ").strip()
            if not label and not e.clickable:
                continue
            x, y = self._to_model(*e.center, shot)
            kind = e.cls.rsplit(".", 1)[-1]
            lines.append(f"- “{label[:30]}” {kind}{' 可点击' if e.clickable else ''} 中心[{x}, {y}]")
            if len(lines) >= limit:
                break
        return "\n".join(lines) if lines else "（没有可识别的元素）"

    def _memory_text(self, device_id: str) -> str:
        turns = self._memory.get(device_id, [])[-self.cfg.memory_turns:] if self.cfg.memory_turns > 0 else []
        if not turns:
            return ""
        return "最近的对话（供参考）：\n" + "\n".join(f"用户：{t.instruction} → 你：{t.reply}" for t in turns) + "\n"

    @staticmethod
    def _prune_images(messages: list[dict[str, Any]], keep: int) -> None:
        """Replace all but the newest `keep` screenshots with a text placeholder."""
        seen = 0
        for msg in reversed(messages):
            content = msg.get("content")
            if msg.get("role") != "user" or not isinstance(content, list):
                continue
            if not any(p.get("type") == "image_url" for p in content):
                continue
            seen += 1
            if seen > keep:
                msg["content"] = [p for p in content if p.get("type") != "image_url"] + [
                    {"type": "text", "text": "（这一步的截图已省略）"}]

    # ------------------------------------------------------------------ actions
    async def _execute(self, name: str, args: dict[str, Any], ctx: AgentContext,
                       shot: Screenshot | None) -> tuple[str, str]:
        """Run one tool call. Returns (display text, result text for the model)."""
        phone = ctx.phone
        target = str(args.get("target") or "").strip()
        if name in ("tap", "long_press"):
            x, y = self._from_model(*point(args), shot)
            if name == "tap":
                await phone.tap(x, y)
                return f"点击 {target}" if target else "点击", "已点击"
            await phone.long_press(x, y)
            return f"长按 {target}" if target else "长按", "已长按"
        if name == "swipe":
            x1, y1 = self._from_model(*point(args, "start_2d", "x1", "y1"), shot)
            x2, y2 = self._from_model(*point(args, "end_2d", "x2", "y2"), shot)
            await phone.swipe(x1, y1, x2, y2, int(args.get("duration_ms", 300)))
            return "滑动", "已滑动"
        if name == "scroll":
            direction = str(args.get("direction", "down"))
            if direction not in ("down", "up", "left", "right"):
                raise ValueError(f"direction 只能是 down/up/left/right，收到 {direction}")
            await phone.scroll(direction)
            return {"down": "向下翻", "up": "向上翻", "left": "向左翻", "right": "向右翻"}[direction], "已滚动"
        if name == "type_text":
            text = str(args["text"])
            method = await phone.input_text(text)
            if args.get("submit"):
                await phone.enter()
            return f"输入 {text}", f"已输入（{method}）" + ("并回车" if args.get("submit") else "")
        if name == "launch_app":
            app = str(args["app"]).strip()
            package = await self.apps.resolve(phone, app)
            if package is None:
                return f"找应用 {app}", f"没有找到应用“{app}”，可以回到桌面找图标，或换个名字"
            await phone.launch_app(package)
            return f"打开 {self.apps.label(package) or app}", f"已启动 {package}"
        if name == "back":
            await phone.back()
            return "返回", "已返回"
        if name == "home":
            await phone.home()
            return "回到桌面", "已回到桌面"
        if name == "wait":
            seconds = min(max(float(args.get("seconds", 2)), 0.5), 5.0)
            await asyncio.sleep(seconds)
            return "等待加载", f"已等待 {seconds:.0f} 秒"
        if name == "phone_info":
            topic = str(args.get("topic", ""))
            text = await phone_info.read(phone, topic, self.apps.label)
            log.info("phone_info %s: %s", topic, text[:160])
            return f"查看{phone_info.TOPICS.get(topic, '手机状态')}", text
        raise ValueError(f"未知工具 {name}")

    async def _fast_launch(self, instruction: str, ctx: AgentContext) -> str:
        """Handle a bare "打开<应用>" without the LLM. '' = not applicable, use the full loop."""
        m = FAST_LAUNCH.match(instruction.strip())
        if not m:
            return ""
        package = await self.apps.resolve(ctx.phone, m.group("app"), exact=True)
        if package is None:  # "打开淘宝搜索耳机" etc.: not just an app name -> full loop
            return ""
        label = self.apps.label(package) or m.group("app")
        await ctx.report("acting", f"打开 {label}")
        await ctx.phone.launch_app(package)
        log.info("fast launch '%s' -> %s", instruction, package)
        reply = f"好了，已经打开{label}"
        turns = self._memory.setdefault(ctx.device_id, [])
        turns.append(_Turn(instruction, reply))
        del turns[:-10]
        return reply

    # ------------------------------------------------------------------ main loop
    async def run(self, instruction: str, ctx: AgentContext) -> str:
        phone: PhoneController = ctx.phone
        await phone.ensure_ready()
        info = await self.model_info()
        try:
            unlocked = await phone.wake_and_unlock()
        except (PhoneError, AdbError) as e:
            log.warning("wake/unlock failed: %s", e)
            unlocked = True  # try anyway; the model sees what is on screen
        if not unlocked:
            raise PhoneUnavailable("手机锁屏了（有锁屏密码）", "locked")
        use_tree = self.cfg.ui_tree == "true" or (self.cfg.ui_tree == "auto" and not info.vision)
        installed = await self.apps.installed_labels(phone)
        if self.cfg.fast_launch:
            reply = await self._fast_launch(instruction, ctx)
            if reply:
                return reply
        messages: list[dict[str, Any]] = [{"role": "system", "content": self._system_prompt(installed)}]
        memory = self._memory_text(ctx.device_id)
        done: list[str] = []
        failures = 0  # consecutive failed actions
        scrolls = scrolls_in_row = refused = 0  # paging budget (MAX_SCROLLS*)
        last_action = ""
        same_screen = 0
        prev_jpeg = b""
        reply = ""
        try:
            for step in range(1, ctx.max_steps + 1):
                ctx.check_cancelled()
                # 1. observe
                shot = await phone.screenshot_info()
                elems: list[UiElement] = []
                if use_tree:
                    try:
                        elems = await phone.ui_elements()
                    except (PhoneError, AdbError) as e:
                        log.warning("ui dump failed: %s", e)
                app = ""
                try:
                    app = await phone.current_app()
                except (PhoneError, AdbError):
                    pass
                same_screen = same_screen + 1 if shot.jpeg == prev_jpeg else 0
                prev_jpeg = shot.jpeg
                lines = []
                if step == 1:
                    lines.append(memory + f"当前时间：{time.strftime('%Y-%m-%d %H:%M %A')}。\n用户的指令：{instruction}")
                lines.append(f"第 {step} 步（最多 {ctx.max_steps} 步）。当前前台应用：{self.apps.label(app) or app or '未知'}。")
                if self.cfg.coords == "pixel":
                    lines.append(f"截图尺寸 {shot.width}x{shot.height}。")
                if last_action:
                    lines.append(f"上一步：{last_action}" + ("（屏幕没有变化）" if same_screen else ""))
                if use_tree:
                    lines.append("屏幕上的元素：\n" + self._describe_elements(elems, shot))
                content: list[dict[str, Any]] = [{"type": "text", "text": "\n".join(lines)}]
                if info.vision:
                    content.append({"type": "image_url", "image_url": {
                        "url": "data:image/jpeg;base64," + base64.b64encode(shot.jpeg).decode()}})
                messages.append({"role": "user", "content": content})
                self._prune_images(messages, max(1, self.cfg.history_images))

                # 2. decide
                ctx.check_cancelled()
                message = await self.chat(messages, info.model)
                call = extract_tool_call(message)
                if call is None:
                    text = (message.get("content") or "").strip()
                    log.info("agent step %d: no tool call, content=%r", step, text[:200])
                    if text:  # the model answered in plain text: treat as the final reply
                        reply = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
                        return reply or "好的"
                    messages.append({"role": "assistant", "content": ""})
                    messages.append({"role": "user", "content": "请调用一个工具。"})
                    continue
                name, args, call_id = call
                log.info("agent step %d '%s' -> %s %s", step, instruction, name,
                         json.dumps(args, ensure_ascii=False)[:200])
                messages.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": [
                    {"id": call_id, "type": "function",
                     "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]})
                if name == "finish":
                    reply = str(args.get("reply") or "").strip() or "好了"
                    return reply

                # 3. act
                if name in ("scroll", "swipe"):
                    if scrolls_in_row >= MAX_SCROLLS_IN_A_ROW or scrolls >= MAX_SCROLLS:
                        refused += 1
                        log.info("agent step %d: paging refused (%d scrolls, %d in a row)", step, scrolls,
                                 scrolls_in_row)
                        if refused > 1:
                            reply = "翻了好几页还没找到，先停下了。"
                            return reply
                        last_action = "翻页（没有执行：翻页次数用完了）"
                        messages.append({"role": "tool", "tool_call_id": call_id, "content": (
                            f"没有执行：已经翻了 {scrolls} 次页，不能再翻了。改用页面里的搜索、换个入口，"
                            "或者调用 finish，把已经看到的信息告诉用户。")})
                        continue
                    scrolls += 1
                    scrolls_in_row += 1
                else:
                    scrolls_in_row = 0
                try:
                    shown, result = await self._execute(name, args, ctx, shot)
                    failures = 0
                except TOOL_ERRORS as e:
                    shown, result = f"{TOOL_LABELS.get(name, name)}失败", f"执行失败：{e}"
                    log.warning("agent step %d %s failed: %s", step, name, e)
                    failures += 1
                    if failures >= MAX_CONSECUTIVE_FAILURES:
                        reply = f"操作连续失败，先停下了：{str(e)[:30]}"
                        return reply
                await ctx.report("acting", f"第 {step} 步：{shown}")
                done.append(shown)
                last_action = f"{shown} → {result}"
                messages.append({"role": "tool", "tool_call_id": call_id, "content": result})
                if name != "wait":
                    await asyncio.sleep(ctx.step_delay_s)
            reply = "步骤太多，先停下了。" + (f"已经{done[-1]}" if done else "")
            return reply
        finally:
            if reply:
                turns = self._memory.setdefault(ctx.device_id, [])
                turns.append(_Turn(instruction, reply))
                del turns[:-10]
