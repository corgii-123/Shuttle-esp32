import base64
import io
import wave

from aiohttp import web
from aiohttp.test_utils import TestServer

from shuttle_server.audio import pcm as pcmlib
from shuttle_server.config import QwenAsrConfig
from shuttle_server.pipeline.asr import QwenASR, clean_qwen_text


def test_clean_qwen_text():
    assert clean_qwen_text("language Chinese<asr_text>打开设置") == "打开设置"
    assert clean_qwen_text("  你好  ") == "你好"
    assert clean_qwen_text("<asr_text>hi</asr_text>") == "hi"


async def fake_asr_server():
    seen = {}

    async def transcriptions(request):
        form = await request.post()
        f = form["file"]
        with wave.open(io.BytesIO(f.file.read())) as w:
            seen["rate"] = w.getframerate()
        seen["model"] = form["model"]
        return web.json_response({"text": "language Chinese<asr_text>打开微信"})

    async def chat(request):
        body = await request.json()
        url = body["messages"][0]["content"][0]["audio_url"]["url"]
        assert url.startswith("data:audio/wav;base64,")
        base64.b64decode(url.split(",", 1)[1])
        return web.json_response({"choices": [{"message": {"content": "回到桌面"}}]})

    app = web.Application()
    app.router.add_post("/v1/audio/transcriptions", transcriptions)
    app.router.add_post("/v1/chat/completions", chat)
    srv = TestServer(app)
    await srv.start_server()
    return srv, seen


async def test_qwen_transcriptions_and_chat():
    srv, seen = await fake_asr_server()
    try:
        base = f"http://127.0.0.1:{srv.port}/v1"
        pcm = pcmlib.tone(200, 500, 16000)
        asr = QwenASR(QwenAsrConfig(base_url=base, api="transcriptions"))
        assert await asr.transcribe(pcm, 16000) == "打开微信"
        assert seen == {"rate": 16000, "model": "mlx-community/Qwen3-ASR-1.7B-8bit"}
        await asr.close()
        asr = QwenASR(QwenAsrConfig(base_url=base, api="chat"))
        assert await asr.transcribe(pcm, 16000) == "回到桌面"
        assert await asr.transcribe(b"", 16000) == ""
        await asr.close()
    finally:
        await srv.close()
