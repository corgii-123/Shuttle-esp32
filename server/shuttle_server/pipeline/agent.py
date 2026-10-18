"""Agent interface: instruction text + PhoneController  ->  loop(screenshot -> decide -> act) -> reply.

Plug your own agent (e.g. your pi agent) in one of three ways:
  * agent.provider: python   -> a class implementing `Agent` (config agent.python.target)
  * agent.provider: command  -> any executable; it drives the phone via the HTTP phone API
                                ($SHUTTLE_PHONE_API, see docs/protocol.md) and prints the reply
  * edit/replace SimpleAgent below (rule-based reference implementation of the loop)
"""
from __future__ import annotations

import abc
import asyncio
import importlib
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..adb.phone import PhoneController, PhoneError, UiElement
from ..config import AgentConfig

log = logging.getLogger(__name__)

ReportFn = Callable[[str, str], Awaitable[None]]


@dataclass
class AgentContext:
    phone: PhoneController
    report: ReportFn  # await ctx.report("acting", "点击 搜索") -> shown on the e-paper
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    device_id: str = ""
    max_steps: int = 15
    step_delay_s: float = 1.0
    phone_api_url: str = ""  # http://127.0.0.1:<port>/api/phone (for out-of-process agents)
    phone_api_token: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def check_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise asyncio.CancelledError("task cancelled by user")


class Agent(abc.ABC):
    name = "agent"

    @abc.abstractmethod
    async def run(self, instruction: str, ctx: AgentContext) -> str:
        """Carry out `instruction` on the phone; return the sentence to speak back."""

    async def close(self) -> None:
        pass


class EchoAgent(Agent):
    """Does not touch the phone - tests voice in/out only."""

    name = "echo"

    async def run(self, instruction: str, ctx: AgentContext) -> str:
        await ctx.report("acting", f"收到：{instruction}")
        return f"你说的是：{instruction}"


# --------------------------------------------------------------------------- SimpleAgent
@dataclass
class Action:
    kind: str  # tap | input | back | home | scroll | launch | enter | wait | done | fail
    arg: Any = None
    say: str = ""  # progress text for the display


COMMON_APPS = {
    "设置": "com.android.settings",
    "微信": "com.tencent.mm",
    "支付宝": "com.eg.android.AlipayGphone",
    "抖音": "com.ss.android.ugc.aweme",
    "淘宝": "com.taobao.taobao",
    "高德地图": "com.autonavi.minimap",
    "地图": "com.autonavi.minimap",
    "美团": "com.sankuai.meituan",
    "哔哩哔哩": "tv.danmaku.bili",
    "b站": "tv.danmaku.bili",
    "qq": "com.tencent.mobileqq",
    "网易云音乐": "com.netease.cloudmusic",
    "小红书": "com.xingin.xhs",
    "chrome": "com.android.chrome",
}

STEP_SPLIT = re.compile(r"(?:，|,|。|然后|接着|再|并且|之后)")


class SimpleAgent(Agent):
    """Reference implementation of the screenshot -> decide -> act loop, without an LLM.

    Understands short Chinese commands, chained with "然后/，": 打开<应用>, 点击<文字>, 输入<文字>,
    搜索<文字>, 返回, 回到桌面, 上滑/下滑, 看看屏幕. Replace `decide()` with an LLM call
    (screenshot JPEG + UI elements + instruction -> Action) to get a real agent.
    """

    name = "simple"

    def plan(self, instruction: str) -> list[str]:
        text = instruction.strip().rstrip("。.!！?？")
        return [s.strip() for s in STEP_SPLIT.split(text) if s.strip()]

    async def decide(self, step: str, elems: list[UiElement], ctx: AgentContext, attempt: int) -> Action:
        s = step.replace("帮我", "").replace("一下", "").strip()
        if re.fullmatch(r"(返回|后退|退出)(上一页|一下)?", s):
            return Action("back", say="返回")
        if re.search(r"(回到|返回)?(桌面|主页|主屏幕)", s):
            return Action("home", say="回到桌面")
        if re.fullmatch(r"(往|向)?上(滑|划|翻)(一下|动)?|下一页", s):
            return Action("scroll", "down", say="向上滑动")
        if re.fullmatch(r"(往|向)?下(滑|划|翻)(一下|动)?|上一页", s):
            return Action("scroll", "up", say="向下滑动")
        if re.fullmatch(r"(看看|查看|读一下)(屏幕|当前页面|界面)?(上有什么)?|截图", s):
            labels = [e.label for e in elems if e.label][:8]
            return Action("done", say="屏幕上有：" + "、".join(labels) if labels else "屏幕上没有可识别的文字")
        m = re.match(r"(?:打开|启动|进入)(.+)", s)
        if m:
            name = m.group(1).strip()
            pkg = COMMON_APPS.get(name.lower()) or COMMON_APPS.get(name)
            if pkg:
                return Action("launch", pkg, say=f"打开{name}")
            target = self._match(name, elems)
            if target is not None:
                return Action("tap", target, say=f"点击 {target.label}")
            if attempt == 0:
                return Action("home", say="回到桌面找应用")
            return Action("fail", say=f"没找到{name}")
        m = re.match(r"(?:点击|点一下|点|按)(.+)", s)
        if m:
            target = self._match(m.group(1).strip(), elems)
            if target is None:
                return Action("fail", say=f"屏幕上没有“{m.group(1).strip()}”")
            return Action("tap", target, say=f"点击 {target.label}")
        m = re.match(r"(?:输入|填写|打字)(.+)", s)
        if m:
            return Action("input", m.group(1).strip(), say=f"输入 {m.group(1).strip()}")
        m = re.match(r"搜索(.+)", s)
        if m:
            box = self._match("搜索", elems)
            if box is not None and attempt == 0:
                return Action("tap", box, say="点击搜索框")
            return Action("input", m.group(1).strip() + "\n", say=f"搜索 {m.group(1).strip()}")
        return Action("fail", say=f"我还不会：{step}")

    @staticmethod
    def _match(label: str, elems: list[UiElement]) -> UiElement | None:
        for e in elems:
            if e.label == label:
                return e
        for e in elems:
            if label and label in e.label:
                return e
        return None

    async def act(self, action: Action, ctx: AgentContext) -> None:
        phone = ctx.phone
        if action.kind == "tap":
            x, y = action.arg.center
            await phone.tap(x, y)
        elif action.kind == "input":
            text: str = action.arg
            await phone.input_text(text.rstrip("\n"))
            if text.endswith("\n"):
                await phone.enter()
        elif action.kind == "back":
            await phone.back()
        elif action.kind == "home":
            await phone.home()
        elif action.kind == "scroll":
            await phone.scroll(action.arg)
        elif action.kind == "launch":
            await phone.launch_app(action.arg)
        elif action.kind == "enter":
            await phone.enter()

    async def run(self, instruction: str, ctx: AgentContext) -> str:
        steps = self.plan(instruction)
        if not steps:
            return "没有听清要做什么"
        done: list[str] = []
        total = 0
        for step in steps:
            for attempt in range(3):
                ctx.check_cancelled()
                total += 1
                if total > ctx.max_steps:
                    return "步骤太多，已停止。" + ("已完成：" + "，".join(done) if done else "")
                # 1. observe
                await ctx.phone.screenshot_info()  # also fixes the coordinate scale
                try:
                    elems = await ctx.phone.ui_elements()
                except PhoneError as e:
                    log.warning("ui dump failed: %s", e)
                    elems = []
                # 2. decide
                action = await self.decide(step, elems, ctx, attempt)
                log.info("agent step %d '%s' -> %s %s", total, step, action.kind, action.say)
                if action.kind == "fail":
                    return action.say + ("。已完成：" + "，".join(done) if done else "")
                await ctx.report("acting", action.say)
                if action.kind == "done":
                    return action.say
                # 3. act
                await self.act(action, ctx)
                await asyncio.sleep(ctx.step_delay_s)
                # "打开X" via launcher may need a second round (home -> tap icon)
                if action.kind == "home" and step.startswith(("打开", "启动", "进入")) and attempt == 0:
                    continue
                if action.kind == "tap" and step.startswith("搜索") and attempt == 0:
                    continue
                done.append(action.say)
                break
        return "好了，" + "，".join(done)


# --------------------------------------------------------------------------- external agents
class CommandAgent(Agent):
    """Runs an external agent process (e.g. your pi agent).

    argv supports {instruction}. Environment given to the process:
      SHUTTLE_INSTRUCTION, SHUTTLE_PHONE_API, SHUTTLE_PHONE_TOKEN, SHUTTLE_DEVICE_ID
    stdout: the final reply (last non-empty line is spoken).
    stderr: lines starting with "STATUS:" are shown on the e-paper as progress.
    """

    name = "command"

    def __init__(self, argv: list[str], cwd: str = "", timeout_s: float = 300.0):
        self.argv = argv
        self.cwd = cwd or None
        self.timeout_s = timeout_s

    async def run(self, instruction: str, ctx: AgentContext) -> str:
        argv = [a.replace("{instruction}", instruction) for a in self.argv]
        env = dict(os.environ, SHUTTLE_INSTRUCTION=instruction, SHUTTLE_PHONE_API=ctx.phone_api_url,
                   SHUTTLE_PHONE_TOKEN=ctx.phone_api_token, SHUTTLE_DEVICE_ID=ctx.device_id)
        proc = await asyncio.create_subprocess_exec(*argv, cwd=self.cwd, env=env, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)

        async def pump_stderr() -> None:
            assert proc.stderr
            async for raw in proc.stderr:
                line = raw.decode(errors="replace").rstrip()
                if line.startswith("STATUS:"):
                    await ctx.report("acting", line[7:].strip())
                elif line:
                    log.info("[agent] %s", line)

        err_task = asyncio.create_task(pump_stderr())
        try:
            out = await asyncio.wait_for(proc.stdout.read(), self.timeout_s)  # type: ignore[union-attr]
            await proc.wait()
        except (asyncio.TimeoutError, asyncio.CancelledError):
            proc.kill()
            await proc.wait()
            raise
        finally:
            err_task.cancel()
        lines = [l.strip() for l in out.decode(errors="replace").splitlines() if l.strip()]
        if proc.returncode != 0:
            raise RuntimeError(f"agent exited with {proc.returncode}: {lines[-1] if lines else ''}")
        return lines[-1] if lines else "完成了"


def load_python_agent(target: str, options: dict[str, Any]) -> Agent:
    module_name, _, cls_name = target.partition(":")
    if not module_name or not cls_name:
        raise ValueError("agent.python.target must look like 'package.module:ClassName'")
    cls = getattr(importlib.import_module(module_name), cls_name)
    agent = cls(**options)
    if not isinstance(agent, Agent):
        raise TypeError(f"{target} is not a shuttle_server.pipeline.agent.Agent")
    return agent


def create_agent(cfg: AgentConfig) -> Agent:
    if cfg.provider == "echo":
        return EchoAgent()
    if cfg.provider == "command":
        return CommandAgent(cfg.command.argv, cfg.command.cwd, cfg.timeout_s)
    if cfg.provider == "python":
        return load_python_agent(cfg.python.target, cfg.python.options)
    if cfg.provider == "llm":
        from .llm_agent import LlmAgent  # late import: llm_agent imports this module

        return LlmAgent(cfg.llm)
    return SimpleAgent()
