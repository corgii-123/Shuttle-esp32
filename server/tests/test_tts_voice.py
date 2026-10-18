"""Spoken-reply length cap and the pitch-shifted ("child") voice."""
import array
import shutil

import pytest

from shuttle_server.audio import pcm as pcmlib
from shuttle_server.pipeline.session import shorten_for_speech
from shuttle_server.pipeline.tts import TTS, PitchShiftTTS

HOT_LIST = ("知乎今日热榜前几名：1 法国发生近年来最严重暴力骚乱；2 上帝让你免费去瑞士滑雪但禁止发朋友圈；"
            "3 贫困生花三千块治牙引发热议；4 网传某模型指纹测试结果；5 杭州 21 岁女生摆摊卖柠檬奶一年开出 20 家门店。")


def test_long_reply_is_cut_at_a_clause_end():
    spoken = shorten_for_speech(HOT_LIST, 60)
    assert len(spoken) <= 61 and spoken.endswith("禁止发朋友圈。")
    assert shorten_for_speech("好了，已经打开设置。", 60) == "好了，已经打开设置。"
    assert shorten_for_speech(HOT_LIST, 0) == HOT_LIST
    assert shorten_for_speech("一" * 100, 60) == "一" * 60 + "。"


def crossings(pcm: bytes) -> int:
    s = array.array("h", pcm)
    return sum(1 for a, b in zip(s, s[1:]) if (a < 0) != (b < 0))


class ToneTTS(TTS):
    name = "tone"

    async def synthesize(self, text):
        return pcmlib.tone(440, 2000, 24000, 0.3), 24000


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
async def test_pitch_shift_keeps_length_and_raises_pitch():
    plain, rate = await ToneTTS().synthesize("")
    shifted, rate2 = await PitchShiftTTS(ToneTTS(), 4).synthesize("")
    assert rate2 == rate
    assert abs(len(shifted) - len(plain)) / 2 / rate < 0.1  # same duration (filters trim a few ms)
    assert 1.2 < crossings(shifted) / crossings(plain) < 1.32  # 4 semitones = x1.26
