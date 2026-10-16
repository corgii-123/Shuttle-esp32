import io

import pytest
from PIL import Image

from conftest import FAKE_ADB, adb_calls
from shuttle_server.adb.adb_cli import AdbCli
from shuttle_server.adb.phone import PhoneController, PhoneError, compress_screenshot, escape_input_text
from shuttle_server.config import PhoneConfig


def noisy_png(w, h):
    import random
    rnd = random.Random(0)
    img = Image.frombytes("RGB", (w, h), bytes(rnd.getrandbits(8) for _ in range(w * h * 3)))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_compress_respects_size_and_short_side():
    png = noisy_png(1080, 2400)  # random noise: worst case for JPEG
    jpeg, w, h = compress_screenshot(png, 720, 200 * 1024)
    assert len(jpeg) <= 200 * 1024
    assert min(w, h) <= 720
    assert jpeg[:2] == b"\xff\xd8"
    # aspect ratio kept
    assert abs(w / h - 1080 / 2400) < 0.01


def test_compress_small_image_not_upscaled():
    png = noisy_png(400, 300)
    _, w, h = compress_screenshot(png, 720, 200 * 1024)
    assert (w, h) == (400, 300)


def test_escape_input_text():
    assert escape_input_text("hello world") == "hello%sworld"
    assert escape_input_text("a'b") == "'a'\"'\"'b'"


@pytest.fixture
def phone(fake_adb_env):
    return PhoneController(AdbCli(FAKE_ADB), "fake-phone", PhoneConfig()), fake_adb_env


async def test_screenshot_and_coordinate_mapping(phone):
    ctl, log = phone
    shot = await ctl.screenshot_info()
    assert shot.device_width == 1080 and shot.device_height == 2400
    assert shot.width == 720 and shot.height == 1600
    assert len(shot.jpeg) <= 200 * 1024
    await ctl.tap(360, 800)  # center of the screenshot
    await ctl.swipe(0, 0, 720, 1600, 250)
    calls = [" ".join(c[2:]) for c in adb_calls(log)]
    assert "shell input tap 540 1200" in calls
    assert "shell input swipe 0 0 1079 2399 250" in calls


async def test_ui_elements_in_screenshot_space(phone):
    ctl, _ = phone
    await ctl.screenshot_info()
    elems = await ctl.ui_elements()
    wechat = next(e for e in elems if e.text == "微信")
    # device bounds [320,300][560,560] * (720/1080)
    assert wechat.bounds == (213, 200, 373, 373)
    found = await ctl.find_element("设置")
    assert found is not None and found.clickable


async def test_input_text_ascii_and_chinese(phone):
    ctl, log = phone
    assert await ctl.input_text("hi there") == "input_text"
    assert ["-s", "fake-phone", "shell", "input text hi%sthere"] in adb_calls(log)
    ctl.helper_jar = ctl.helper_jar.with_name("missing.jar")
    with pytest.raises(PhoneError):
        await ctl.input_text("你好")  # no helper jar and no ADBKeyboard on the fake phone


async def test_keys(phone):
    ctl, log = phone
    await ctl.back()
    await ctl.home()
    await ctl.launch_app("com.android.settings")
    cmds = [c[3] for c in adb_calls(log) if c[:3] == ["-s", "fake-phone", "shell"]]
    assert "input keyevent 4" in cmds and "input keyevent 3" in cmds
    assert any(c.startswith("monkey -p com.android.settings") for c in cmds)


async def test_phone_side_screenshot_falls_back(phone):
    ctl, log = phone
    ctl.cfg.device_side_screenshot = True
    ctl.helper_jar = ctl.helper_jar.with_name("missing.jar")
    shot = await ctl.screenshot_info()
    assert shot.source == "mac"
