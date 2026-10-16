"""Central configuration (config.yaml). Every tunable lives here.

Load order: dataclass defaults <- YAML file <- environment overrides
(SHUTTLE_TOKEN, SHUTTLE_PORT, SHUTTLE_LOG_DIR, SHUTTLE_ASR, SHUTTLE_AGENT, SHUTTLE_TTS).
"""
from __future__ import annotations

import dataclasses
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints

import yaml

log = logging.getLogger(__name__)


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"  # cloudflared connects locally; use 0.0.0.0 for LAN tests
    port: int = 8765
    public_url: str = ""  # e.g. wss://shuttle.example.com (informational, OTA)
    ctrl_heartbeat_s: float = 30.0  # WS ping interval on /ws/ctrl (0 = off); xiaozhi answers pings
    adb_heartbeat_s: float = 20.0  # WS ping interval on /ws/adb (0 = off)


@dataclass
class AuthConfig:
    token: str = "change-me"
    allow_query_token: bool = False  # also accept ?token=... (handy for curl/browser)


@dataclass
class LoggingConfig:
    dir: str = "~/Library/Logs/shuttle"
    level: str = "INFO"
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 10
    console: bool = True


@dataclass
class AdbConfig:
    path: str = "adb"
    bind_host: str = "127.0.0.1"
    local_port: int = 15555  # first device; further devices get +1, +2, ...
    connect_interval_s: float = 5.0  # supervisor: re-run `adb connect` when not "device"
    device_grace_s: float = 60.0  # keep listener + adb entry this long after the device left
    pair_wait_s: float = 10.0  # adb TCP connection waits this long for a device WS
    phone_ready_timeout_s: float = 3.0  # wait for device's {"type":"phone","state":"connected"}
    command_timeout_s: float = 30.0
    offline_reset_s: float = 30.0  # "offline" this long while paired -> adb disconnect/connect
    # Use this adb serial when no ESP32 tunnel is up (e.g. phone on USB / same LAN while
    # testing with tools/sim_device.py). Empty = none.
    fallback_serial: str = ""


@dataclass
class PhoneConfig:
    screenshot_short_side: int = 720
    screenshot_max_bytes: int = 200 * 1024
    jpeg_quality: int = 80
    jpeg_min_quality: int = 35
    device_side_screenshot: bool = False  # run phone_helper.jar via app_process on the phone
    helper_jar: str = ""  # default: shuttle_server/phone_helper/shuttle-helper.jar
    input_method: str = "auto"  # auto | clipboard | adbkeyboard | input_text
    ui_dump_timeout_s: float = 20.0


@dataclass
class PhonesConfig:
    """Several phones: the ESP32 finds them (scan for adbd), the Mac recognises and picks one."""
    state_file: str = "~/Library/Application Support/shuttle/phones.json"  # "" = memory only
    # A phone that was never seen before shows up (someone just ran `adb tcpip 5555` on it):
    # switch to it automatically when no task is running.
    auto_switch_new: bool = True
    voice_commands: bool = True  # "换手机" / "切换到<名字>" / "现在连的是哪台手机" / "关闭无线调试"
    aliases: dict[str, str] = field(default_factory=dict)  # serial -> spoken name, e.g. {"R5CT...": "工作机"}
    rescan_wait_s: float = 8.0  # "换手机" with one phone: ask the device to rescan, wait this long
    avoid_after_fail_s: float = 120.0  # an unreachable / switched-off host is not picked again for this long


@dataclass
class VoiceConfig:
    uplink_sample_rate: int = 16000  # device mic (xiaozhi hello)
    tts_sample_rate: int = 24000  # downlink; matches the board's codec output rate
    frame_duration_ms: int = 60
    tts_lead_frames: int = 5  # frames sent ahead of real time
    min_utterance_ms: int = 400
    max_utterance_s: int = 60
    keepalive_s: float = 30.0  # "shuttle ping" keeps xiaozhi's 120 s channel timeout away
    idle_close_s: float = 600.0  # close an idle ctrl channel (device re-opens on next press)
    cancel_on_new_utterance: bool = True
    vad_silence_ms: int = 800  # only for listen mode "auto" (stock xiaozhi devices)
    vad_threshold: int = 500


@dataclass
class QwenAsrConfig:
    base_url: str = "http://127.0.0.1:8000/v1"
    api: str = "transcriptions"  # transcriptions (/audio/transcriptions) | chat (/chat/completions)
    model: str = "Qwen/Qwen3-ASR-1.7B"
    api_key: str = "EMPTY"
    language: str = "zh"
    prompt: str = ""
    timeout_s: float = 60.0


@dataclass
class MockAsrConfig:
    text: str = "打开设置"


@dataclass
class AsrConfig:
    provider: str = "qwen"  # qwen | mock
    qwen: QwenAsrConfig = field(default_factory=QwenAsrConfig)
    mock: MockAsrConfig = field(default_factory=MockAsrConfig)


@dataclass
class CommandAgentConfig:
    # {instruction} is substituted. The process gets SHUTTLE_PHONE_API / SHUTTLE_PHONE_TOKEN
    # env vars so it can drive the phone through the HTTP phone API.
    argv: list[str] = field(default_factory=lambda: ["pi", "-p", "{instruction}"])
    cwd: str = ""


@dataclass
class PythonAgentConfig:
    target: str = ""  # "module.path:ClassName" implementing shuttle_server.pipeline.agent.Agent
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentConfig:
    provider: str = "simple"  # simple | echo | command | python
    max_steps: int = 15
    step_delay_s: float = 1.0
    timeout_s: float = 300.0
    command: CommandAgentConfig = field(default_factory=CommandAgentConfig)
    python: PythonAgentConfig = field(default_factory=PythonAgentConfig)


@dataclass
class SayTtsConfig:
    voice: str = "Tingting"  # `say -v '?'` lists voices; Tingting = zh_CN
    rate: int = 200


@dataclass
class OpenAiTtsConfig:
    base_url: str = "http://127.0.0.1:8001/v1"
    model: str = "tts-1"
    voice: str = "alloy"
    api_key: str = "EMPTY"
    timeout_s: float = 60.0


@dataclass
class CommandTtsConfig:
    argv: list[str] = field(default_factory=lambda: ["my-tts", "--text", "{text}", "--out", "{out}"])


@dataclass
class TtsConfig:
    provider: str = "say"  # say | mock | openai | command
    say: SayTtsConfig = field(default_factory=SayTtsConfig)
    openai: OpenAiTtsConfig = field(default_factory=OpenAiTtsConfig)
    command: CommandTtsConfig = field(default_factory=CommandTtsConfig)


@dataclass
class OtaConfig:
    enabled: bool = True
    timezone_offset_min: int = 480
    firmware_dir: str = ""  # optional: dir with <version>.bin files for remote OTA
    expose_websocket_url: bool = False  # include websocket.url in OTA reply (stock xiaozhi)


@dataclass
class PhoneApiConfig:
    enabled: bool = True  # /api/phone/* (token-protected) for external agents (pi)


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    adb: AdbConfig = field(default_factory=AdbConfig)
    phone: PhoneConfig = field(default_factory=PhoneConfig)
    phones: PhonesConfig = field(default_factory=PhonesConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)
    ota: OtaConfig = field(default_factory=OtaConfig)
    phone_api: PhoneApiConfig = field(default_factory=PhoneApiConfig)

    def redacted(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["auth"]["token"] = mask(self.auth.token)
        d["asr"]["qwen"]["api_key"] = mask(self.asr.qwen.api_key)
        d["tts"]["openai"]["api_key"] = mask(self.tts.openai.api_key)
        return d


def mask(secret: str) -> str:
    if not secret or len(secret) <= 4:
        return "****"
    return f"{secret[:4]}...({len(secret)})"


class ConfigError(ValueError):
    pass


def _coerce(value: Any, typ: Any, path: str) -> Any:
    origin = get_origin(typ)
    if dataclasses.is_dataclass(typ):
        if not isinstance(value, dict):
            raise ConfigError(f"{path}: expected a mapping")
        return _build(typ, value, path)
    if origin is list:
        if not isinstance(value, list):
            raise ConfigError(f"{path}: expected a list")
        (inner,) = get_args(typ) or (Any,)
        return [_coerce(v, inner, f"{path}[{i}]") for i, v in enumerate(value)]
    if origin is dict:
        if not isinstance(value, dict):
            raise ConfigError(f"{path}: expected a mapping")
        return dict(value)
    if typ is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("1", "true", "yes", "on", "0", "false", "no", "off"):
            return value.lower() in ("1", "true", "yes", "on")
        raise ConfigError(f"{path}: expected true/false")
    if typ is int:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ConfigError(f"{path}: expected an integer")
        return int(value)
    if typ is float:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ConfigError(f"{path}: expected a number")
        return float(value)
    if typ is str:
        if value is None:
            return ""
        return str(value)
    return value


def _build(cls: type, data: dict[str, Any], path: str) -> Any:
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    names = {f.name for f in dataclasses.fields(cls)}
    for key, value in data.items():
        if key not in names:
            log.warning("config: unknown key %s.%s (ignored)", path, key)
            continue
        kwargs[key] = _coerce(value, hints[key], f"{path}.{key}")
    return cls(**kwargs)


ENV_OVERRIDES = {
    "SHUTTLE_TOKEN": ("auth", "token"),
    "SHUTTLE_HOST": ("server", "host"),
    "SHUTTLE_PORT": ("server", "port"),
    "SHUTTLE_LOG_DIR": ("logging", "dir"),
    "SHUTTLE_LOG_LEVEL": ("logging", "level"),
    "SHUTTLE_ADB": ("adb", "path"),
    "SHUTTLE_ASR": ("asr", "provider"),
    "SHUTTLE_AGENT": ("agent", "provider"),
    "SHUTTLE_TTS": ("tts", "provider"),
}


def load_config(path: str | os.PathLike[str] | None = None, env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    raw: dict[str, Any] = {}
    if path:
        p = Path(path).expanduser()
        if not p.exists():
            raise ConfigError(f"config file not found: {p}")
        loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"{p}: top level must be a mapping")
        raw = loaded
    for var, (section, key) in ENV_OVERRIDES.items():
        if env.get(var):
            raw.setdefault(section, {})[key] = env[var]
    cfg: Config = _build(Config, raw, "config")
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    if cfg.asr.provider not in ("qwen", "mock"):
        raise ConfigError(f"asr.provider must be qwen|mock, got {cfg.asr.provider!r}")
    if cfg.agent.provider not in ("simple", "echo", "command", "python"):
        raise ConfigError(f"agent.provider must be simple|echo|command|python, got {cfg.agent.provider!r}")
    if cfg.tts.provider not in ("say", "mock", "openai", "command"):
        raise ConfigError(f"tts.provider must be say|mock|openai|command, got {cfg.tts.provider!r}")
    if cfg.asr.qwen.api not in ("transcriptions", "chat"):
        raise ConfigError("asr.qwen.api must be transcriptions|chat")
    if cfg.phone.input_method not in ("auto", "clipboard", "adbkeyboard", "input_text"):
        raise ConfigError("phone.input_method must be auto|clipboard|adbkeyboard|input_text")
    if cfg.voice.frame_duration_ms not in (20, 40, 60):
        raise ConfigError("voice.frame_duration_ms must be 20, 40 or 60")
    if cfg.voice.tts_sample_rate not in (8000, 12000, 16000, 24000, 48000):
        raise ConfigError("voice.tts_sample_rate must be an Opus rate (8000/12000/16000/24000/48000)")
    if not cfg.auth.token or cfg.auth.token == "change-me":
        log.warning("auth.token is not set (still 'change-me') - set a long random token!")
