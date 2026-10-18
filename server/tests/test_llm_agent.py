"""LlmAgent against a fake OpenAI-compatible server (scripted tool calls) and the fake adb phone."""
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from conftest import FAKE_ADB, adb_calls
from shuttle_server.adb.adb_cli import AdbCli
from shuttle_server.adb.phone import PhoneController
from shuttle_server.config import LlmAgentConfig, PhoneConfig
from shuttle_server.pipeline.agent import AgentContext
from shuttle_server.pipeline.llm_agent import LlmAgent, extract_tool_call, point


def tool_message(name, args):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"call_{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


class FakeLLM:
    def __init__(self, replies, vision=True):
        self.replies = list(replies)
        self.vision = vision
        self.requests = []

    async def models(self, request):
        return web.json_response({"data": [{"id": "fake-vl", "vision": self.vision,
                                            "input_modalities": ["text", "image"] if self.vision else ["text"]}]})

    async def chat(self, request):
        body = await request.json()
        self.requests.append(body)
        message = self.replies.pop(0) if self.replies else tool_message("finish", {"reply": "没事做了"})
        return web.json_response({"choices": [{"message": message}], "usage": {"prompt_tokens": 1}})


@pytest.fixture
async def llm_factory():
    servers = []

    async def start(replies, vision=True):
        fake = FakeLLM(replies, vision)
        app = web.Application()
        app.router.add_get("/v1/models", fake.models)
        app.router.add_post("/v1/chat/completions", fake.chat)
        srv = TestServer(app, host="127.0.0.1")
        await srv.start_server()
        servers.append(srv)
        return fake, f"http://127.0.0.1:{srv.port}/v1"

    yield start
    for s in servers:
        await s.close()


def make_ctx(fake_adb_env):
    phone = PhoneController(AdbCli(FAKE_ADB), "fake-phone", PhoneConfig())
    reports = []

    async def report(state, text):
        reports.append((state, text))

    return AgentContext(phone=phone, report=report, device_id="dev", max_steps=5, step_delay_s=0), reports


async def test_vision_agent_taps_launches_and_finishes(llm_factory, fake_adb_env):
    fake, url = await llm_factory([
        tool_message("launch_app", {"app": "淘宝"}),
        tool_message("tap", {"point_2d": [500, 500], "target": "搜索框"}),
        tool_message("finish", {"reply": "好了，已经打开淘宝"}),
    ])
    agent = LlmAgent(LlmAgentConfig(base_url=url))
    ctx, reports = make_ctx(fake_adb_env)
    try:
        assert await agent.run("打开淘宝点一下搜索框", ctx) == "好了，已经打开淘宝"
    finally:
        await agent.close()
    cmds = [" ".join(c[2:]) for c in adb_calls(fake_adb_env)]
    assert any("monkey -p com.taobao.taobao" in c for c in cmds)
    assert "shell input tap 540 1200" in cmds  # 0..1000 grid -> centre of the 1080x2400 screen
    assert reports == [("acting", "第 1 步：打开 淘宝"), ("acting", "第 2 步：点击 搜索框")]
    # model saw the installed apps and one screenshot per request (older ones pruned)
    first = fake.requests[0]
    assert "淘宝" in first["messages"][0]["content"] and "微信" in first["messages"][0]["content"]
    last = fake.requests[-1]["messages"]
    images = [p for m in last if isinstance(m.get("content"), list) for p in m["content"]
              if p.get("type") == "image_url"]
    assert len(images) == 1
    assert any(m.get("role") == "tool" for m in last)


async def test_text_only_model_gets_ui_tree(llm_factory, fake_adb_env):
    fake, url = await llm_factory([tool_message("finish", {"reply": "看到了设置"})], vision=False)
    agent = LlmAgent(LlmAgentConfig(base_url=url))
    ctx, _ = make_ctx(fake_adb_env)
    try:
        assert await agent.run("屏幕上有什么", ctx) == "看到了设置"
    finally:
        await agent.close()
    user = fake.requests[0]["messages"][1]["content"]
    assert all(p["type"] == "text" for p in user)
    assert "“设置”" in user[0]["text"]  # uiautomator element list instead of the image


async def test_unknown_app_and_tool_errors_are_reported_to_the_model(llm_factory, fake_adb_env):
    fake, url = await llm_factory([
        tool_message("launch_app", {"app": "不存在的应用"}),
        tool_message("scroll", {"direction": "sideways"}),
        tool_message("finish", {"reply": "没找到"}),
    ])
    agent = LlmAgent(LlmAgentConfig(base_url=url))
    ctx, _ = make_ctx(fake_adb_env)
    try:
        assert await agent.run("打开不存在的应用", ctx) == "没找到"
    finally:
        await agent.close()
    tool_results = [m["content"] for m in fake.requests[-1]["messages"] if m.get("role") == "tool"]
    assert "没有找到应用" in tool_results[0]
    assert tool_results[1].startswith("执行失败")


async def test_step_limit(llm_factory, fake_adb_env):
    fake, url = await llm_factory([tool_message("back", {})] * 10)
    agent = LlmAgent(LlmAgentConfig(base_url=url))
    ctx, _ = make_ctx(fake_adb_env)
    try:
        reply = await agent.run("一直返回", ctx)
    finally:
        await agent.close()
    assert reply.startswith("步骤太多")
    assert len(fake.requests) == 5


def test_extract_tool_call_fallbacks():
    assert extract_tool_call(tool_message("tap", {"x": 1, "y": 2}))[:2] == ("tap", {"x": 1, "y": 2})
    raw = {"content": '<tool_call>\n{"name": "home", "arguments": {}}\n</tool_call>'}
    assert extract_tool_call(raw)[:2] == ("home", {})
    fenced = {"content": '```json\n{"name": "finish", "arguments": {"reply": "好"}}\n```'}
    assert extract_tool_call(fenced)[:2] == ("finish", {"reply": "好"})
    assert extract_tool_call({"content": "我已经完成了"}) is None


async def test_fast_launch_skips_the_llm(llm_factory, fake_adb_env):
    fake, url = await llm_factory([])
    agent = LlmAgent(LlmAgentConfig(base_url=url))
    ctx, reports = make_ctx(fake_adb_env)
    try:
        assert await agent.run("帮我打开微信。", ctx) == "好了，已经打开微信"
        assert fake.requests == []
        assert reports == [("acting", "打开 微信")]
        cmds = [" ".join(c[2:]) for c in adb_calls(fake_adb_env)]
        assert any("monkey -p com.tencent.mm" in c for c in cmds)
        # not a bare app name -> the full screenshot loop runs
        await agent.run("打开微信给张三发消息", ctx)
        assert len(fake.requests) == 1
    finally:
        await agent.close()


def test_point_accepts_what_models_send():
    assert point({"point_2d": [347, 508]}) == (347.0, 508.0)
    assert point({"x": [500, 545], "y": [500, 545]}) == (500.0, 545.0)  # pair stuffed into x (Qwen3.8)
    assert point({"start_2d": [1, 2]}, "start_2d", "x1", "y1") == (1.0, 2.0)
    assert point({"x": 347, "y": 508}) == (347.0, 508.0)
    assert point({"x": [347], "y": [508]}) == (347.0, 508.0)  # seen from Qwen3.8 on Telegram
    assert point({"x": "347", "y": "508px"}) == (347.0, 508.0)
    assert point({"x": [347, 508]}) == (347.0, 508.0)
    assert point({"x": "347, 508"}) == (347.0, 508.0)
    with pytest.raises(ValueError):
        point({"x": 1})


async def test_stops_after_repeated_failures(llm_factory, fake_adb_env):
    fake, url = await llm_factory([tool_message("tap", {"x": "left", "y": "top"})] * 10)
    agent = LlmAgent(LlmAgentConfig(base_url=url))
    ctx, _ = make_ctx(fake_adb_env)
    try:
        reply = await agent.run("点一下左上角", ctx)
    finally:
        await agent.close()
    assert reply.startswith("操作连续失败")
    assert len(fake.requests) == 3


async def test_paging_budget_stops_a_lost_model(llm_factory, fake_adb_env):
    from shuttle_server.pipeline.llm_agent import MAX_SCROLLS_IN_A_ROW
    fake, url = await llm_factory([tool_message("scroll", {"direction": "down"})] * 20)
    agent = LlmAgent(LlmAgentConfig(base_url=url))
    ctx, reports = make_ctx(fake_adb_env)
    ctx.max_steps = 15
    try:
        reply = await agent.run("找一下存储", ctx)
    finally:
        await agent.close()
    assert reply.startswith("翻了好几页")
    # MAX_SCROLLS_IN_A_ROW pages, one refusal (told to search or finish), then it stops
    assert len(fake.requests) == MAX_SCROLLS_IN_A_ROW + 2
    assert sum(1 for state, text in reports if "翻" in text) == MAX_SCROLLS_IN_A_ROW
    refusal = [m for m in fake.requests[-1]["messages"] if m["role"] == "tool"][-1]["content"]
    assert refusal.startswith("没有执行") and "finish" in refusal


async def test_phone_info_answers_without_navigating(llm_factory, fake_adb_env):
    fake, url = await llm_factory([tool_message("phone_info", {"topic": "battery"}),
                                   tool_message("finish", {"reply": "电量 60%，正在充电"})])
    agent = LlmAgent(LlmAgentConfig(base_url=url))
    ctx, reports = make_ctx(fake_adb_env)
    try:
        reply = await agent.run("电量多少", ctx)
    finally:
        await agent.close()
    assert reply == "电量 60%，正在充电"
    result = [m for m in fake.requests[1]["messages"] if m["role"] == "tool"][0]["content"]
    assert "电量 60%" in result and "正在充电（USB）" in result and "30.5°C" in result
    assert ("acting", "第 1 步：查看电池信息") in reports
    tools = {t["function"]["name"] for t in fake.requests[0]["tools"]}
    assert "phone_info" in tools
