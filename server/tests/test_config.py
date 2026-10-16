import pytest

from shuttle_server.config import ConfigError, load_config


def test_defaults_without_file():
    cfg = load_config(None, env={})
    assert cfg.server.port == 8765
    assert cfg.phone.screenshot_max_bytes == 200 * 1024
    assert cfg.asr.provider == "qwen"


def test_example_config_loads():
    from pathlib import Path
    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    cfg = load_config(example, env={})
    assert cfg.voice.tts_sample_rate == 24000


def test_env_overrides(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("auth: {token: fromfile}\nserver: {port: 1234}\n")
    cfg = load_config(p, env={"SHUTTLE_TOKEN": "fromenv", "SHUTTLE_ASR": "mock"})
    assert cfg.auth.token == "fromenv"
    assert cfg.server.port == 1234
    assert cfg.asr.provider == "mock"


def test_bad_values(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("asr: {provider: whisper}\n")
    with pytest.raises(ConfigError):
        load_config(p, env={})
    p.write_text("server: {port: notanumber}\n")
    with pytest.raises((ConfigError, ValueError)):
        load_config(p, env={})


def test_redacted_hides_token():
    cfg = load_config(None, env={"SHUTTLE_TOKEN": "supersecrettoken"})
    assert "supersecrettoken" not in str(cfg.redacted())
