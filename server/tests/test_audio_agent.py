import asyncio

from shuttle_server.audio import pcm as pcmlib
from shuttle_server.audio.opus import OpusDecoder, OpusEncoder
from shuttle_server.pipeline.agent import CommandAgent, SimpleAgent


def test_opus_roundtrip_and_resample():
    pcm = pcmlib.tone(440, 600, 24000)
    enc, dec = OpusEncoder(24000), OpusDecoder(24000)
    out = b"".join(dec.decode(enc.encode(f, 1440)) for f in pcmlib.frames(pcm, 1440))
    assert len(out) == len(pcm)
    assert pcmlib.rms(out) > 1000
    assert len(pcmlib.resample(pcm, 24000, 16000)) == len(pcm) * 2 // 3 // 2 * 2


def test_wav_roundtrip():
    pcm = pcmlib.tone(300, 100, 16000)
    back, rate = pcmlib.read_wav(pcmlib.wav_bytes(pcm, 16000))
    assert rate == 16000 and back == pcm


def test_simple_agent_plan():
    a = SimpleAgent()
    assert a.plan("打开设置，然后点击 WLAN。") == ["打开设置", "点击 WLAN"]


async def test_command_agent(tmp_path):
    script = tmp_path / "agent.sh"
    script.write_text('#!/bin/sh\necho "STATUS: working on $SHUTTLE_INSTRUCTION" >&2\necho "done: $1"\n')
    script.chmod(0o755)
    reports = []

    async def report(state, text):
        reports.append((state, text))

    from shuttle_server.pipeline.agent import AgentContext

    ctx = AgentContext(phone=None, report=report)  # type: ignore[arg-type]
    agent = CommandAgent([str(script), "{instruction}"], timeout_s=10)
    assert await agent.run("打开设置", ctx) == "done: 打开设置"
    await asyncio.sleep(0.05)
    assert ("acting", "working on 打开设置") in reports
