"""Whole chain without hardware: sim device -> /ws/ctrl -> ASR(mock) -> SimpleAgent -> fake adb -> TTS(mock)."""
import aiohttp

from conftest import TOKEN, adb_calls
from shuttle_server.audio import pcm as pcmlib
from sim_device import CtrlClient


async def run_turn(srv, pcm, device="sim-1"):
    async with aiohttp.ClientSession() as http:
        ctrl = CtrlClient(http, srv.url, TOKEN, device)
        await ctrl.connect()
        try:
            return await ctrl.utterance(pcm, realtime=False, timeout_s=20)
        finally:
            await ctrl.close()


async def test_voice_to_phone_to_voice(server_factory, fake_adb_env):
    srv = await server_factory(asr={"mock": {"text": "打开设置，然后点击微信"}})
    res = await run_turn(srv, pcmlib.tone(300, 1500, 16000))
    assert res.stt == "打开设置，然后点击微信"
    states = [s for s, _ in res.statuses]
    assert states[:2] == ["recognizing", "thinking"] and "acting" in states and states[-1] == "done"
    assert res.tts_frames > 5 and len(res.tts_pcm) > 0
    assert res.tts_text and res.tts_text[0].startswith("好了")
    cmds = [" ".join(c) for c in adb_calls(fake_adb_env)]
    assert any("monkey -p com.android.settings" in c for c in cmds)
    assert any("input tap" in c for c in cmds)
    assert any("screencap -p" in c for c in cmds)


async def test_short_press_is_ignored(server_factory):
    srv = await server_factory()
    res = await run_turn(srv, pcmlib.tone(300, 120, 16000))
    assert res.final_state == "idle"
    assert res.stt == ""


async def test_empty_asr_says_not_heard(server_factory):
    srv = await server_factory(asr={"mock": {"text": ""}})
    res = await run_turn(srv, pcmlib.tone(300, 1000, 16000))
    assert ("error", "没听清") in res.statuses
    assert res.tts_frames > 0


async def test_echo_agent_and_unknown_command(server_factory):
    srv = await server_factory(agent={"provider": "echo"})
    res = await run_turn(srv, pcmlib.tone(300, 800, 16000))
    assert res.final_state == "done" and "打开设置" in res.statuses[-1][1]


async def test_no_phone_reports_error(server_factory):
    srv = await server_factory(adb={"fallback_serial": ""})
    res = await run_turn(srv, pcmlib.tone(300, 800, 16000))
    assert ("error", "手机未连接") in res.statuses


async def test_hello_and_protocol_v3_framing(server_factory):
    srv = await server_factory()
    async with aiohttp.ClientSession() as http:
        headers = {"Authorization": f"Bearer {TOKEN}", "Device-Id": "v3dev", "Protocol-Version": "3"}
        async with http.ws_connect(srv.url + "/ws/ctrl", headers=headers) as ws:
            await ws.send_json({"type": "hello", "version": 3, "transport": "websocket",
                                "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1,
                                                 "frame_duration": 60}})
            hello = await ws.receive_json(timeout=5)
            assert hello["type"] == "hello" and hello["transport"] == "websocket"
            assert hello["audio_params"]["sample_rate"] == 24000
            assert hello["session_id"]


async def test_health_and_ota(server_factory):
    srv = await server_factory()
    base = srv.url.replace("ws://", "http://")
    async with aiohttp.ClientSession() as http:
        async with http.get(base + "/health") as r:
            body = await r.json()
            assert r.status == 200 and body["status"] == "ok"
            assert TOKEN not in await r.text()
        async with http.post(base + "/xiaozhi/ota/", json={"application": {"version": "2.5.3"}}) as r:
            ota = await r.json()
            assert ota["firmware"]["version"] == "2.5.3"
            assert ota["websocket"] == {}
            assert "timestamp" in ota["server_time"]
        async with http.get(base + "/status", headers={"Authorization": f"Bearer {TOKEN}"}) as r:
            st = await r.json()
            assert st["providers"] == {"asr": "mock", "agent": "simple", "tts": "mock"}
            assert TOKEN not in str(st)


async def test_phone_api(server_factory):
    srv = await server_factory()
    base = srv.url.replace("ws://", "http://") + "/api/phone"
    h = {"Authorization": f"Bearer {TOKEN}"}
    async with aiohttp.ClientSession() as http:
        async with http.get(base + "/screenshot", headers=h) as r:
            data = await r.read()
            assert r.status == 200 and data[:2] == b"\xff\xd8" and r.headers["X-Width"] == "720"
        async with http.post(base + "/tap", json={"x": 10, "y": 20}, headers=h) as r:
            assert (await r.json())["ok"]
        async with http.get(base + "/ui", headers=h) as r:
            elems = (await r.json())["elements"]
            assert any(e["text"] == "设置" for e in elems)
        async with http.post(base + "/tap", json={}, headers=h) as r:
            assert r.status == 400
        async with http.get(base + "/screenshot") as r:
            assert r.status == 401


async def test_phone_question_is_answered_without_agent(server_factory, fake_adb_env):
    srv = await server_factory(asr={"mock": {"text": "现在连的是哪台手机？"}})
    res = await run_turn(srv, pcmlib.tone(300, 1500, 16000))
    assert res.final_state == "done"
    assert res.tts_text and "手机" in res.tts_text[0]
    # the agent never ran: no screenshot / input on the phone
    assert not any("screencap" in " ".join(c) for c in adb_calls(fake_adb_env))
