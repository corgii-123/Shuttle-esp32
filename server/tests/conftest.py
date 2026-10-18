import asyncio
import json
import os
import socket
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestServer

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "server"))
sys.path.insert(0, str(REPO / "tools"))

from shuttle_server.app import build_app, get_shuttle  # noqa: E402
from shuttle_server.config import load_config  # noqa: E402

FAKE_ADB = f"{sys.executable} {REPO / 'tools' / 'fake_adb.py'}"
TOKEN = "test-token-0123456789"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake_adb_env(tmp_path, monkeypatch):
    log = tmp_path / "adb.log"
    monkeypatch.setenv("FAKE_ADB_LOG", str(log))
    monkeypatch.setenv("FAKE_ADB_STATE", str(tmp_path / "adb_state.json"))
    return log


def adb_calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(l)["argv"] for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]


def make_config(tmp_path, **overrides):
    raw = {
        "server": {"host": "127.0.0.1", "port": 0},
        "auth": {"token": TOKEN},
        "logging": {"dir": "", "console": True, "level": "DEBUG"},
        "adb": {"path": FAKE_ADB, "fallback_serial": "fake-phone", "local_port": free_port(),
                "connect_interval_s": 0.2, "device_grace_s": 1.0, "pair_wait_s": 3.0,
                "phone_ready_timeout_s": 0.5},
        "asr": {"provider": "mock", "mock": {"text": "打开设置"}},
        "agent": {"provider": "simple", "step_delay_s": 0.0},
        "tts": {"provider": "mock"},
        "voice": {"keepalive_s": 0.5},
        "phones": {"state_file": str(tmp_path / "phones.json"), "rescan_wait_s": 2.0},
    }
    for section, values in overrides.items():
        raw.setdefault(section, {}).update(values)
    import yaml
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return load_config(p, env={})


@pytest_asyncio.fixture
async def server_factory(tmp_path, fake_adb_env):
    servers = []

    async def start(**overrides):
        cfg = make_config(tmp_path, **overrides)
        app = build_app(cfg)
        srv = TestServer(app, host="127.0.0.1")
        await srv.start_server()
        cfg.server.port = srv.port
        servers.append(srv)
        srv.url = f"ws://127.0.0.1:{srv.port}"
        srv.shuttle = get_shuttle(app)
        return srv

    yield start
    for s in servers:
        await s.close()
