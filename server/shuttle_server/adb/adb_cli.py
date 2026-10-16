"""Async wrapper around the native `adb` binary (the Mac does all ADB protocol work)."""
from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass

log = logging.getLogger(__name__)


class AdbError(RuntimeError):
    pass


@dataclass
class AdbResult:
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    def check(self, what: str) -> "AdbResult":
        if self.returncode != 0:
            err = self.stderr.decode("utf-8", errors="replace").strip() or self.text.strip()
            raise AdbError(f"{what} failed (rc={self.returncode}): {err[:300]}")
        return self


class AdbCli:
    """Runs `adb` commands. Every call has a timeout so a wedged adb never blocks the server."""

    def __init__(self, path: str = "adb", timeout_s: float = 30.0):
        self.path = path  # may be a command line, e.g. "python3 tools/fake_adb.py"
        self.base_argv = shlex.split(path) or ["adb"]
        self.timeout_s = timeout_s

    async def run(self, *args: str, serial: str | None = None, input_data: bytes | None = None,
                  timeout_s: float | None = None) -> AdbResult:
        argv = list(self.base_argv)
        if serial:
            argv += ["-s", serial]
        argv += list(args)
        timeout = timeout_s or self.timeout_s
        log.debug("adb: %s", " ".join(shlex.quote(a) for a in argv))
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if input_data is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise AdbError(f"adb binary not found: {self.path} (brew install android-platform-tools)") from e
        try:
            out, err = await asyncio.wait_for(proc.communicate(input_data), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise AdbError(f"adb {' '.join(args[:3])} timed out after {timeout}s") from None
        return AdbResult(proc.returncode or 0, out, err)

    # ---- convenience ----
    async def shell(self, serial: str, command: str, timeout_s: float | None = None) -> AdbResult:
        return await self.run("shell", command, serial=serial, timeout_s=timeout_s)

    async def exec_out(self, serial: str, command: str, timeout_s: float | None = None) -> bytes:
        res = await self.run("exec-out", command, serial=serial, timeout_s=timeout_s)
        res.check(f"exec-out {command}")
        return res.stdout

    async def connect(self, addr: str) -> str:
        res = await self.run("connect", addr, timeout_s=15)
        return (res.text + res.stderr.decode(errors="replace")).strip()

    async def disconnect(self, addr: str) -> str:
        res = await self.run("disconnect", addr, timeout_s=10)
        return (res.text + res.stderr.decode(errors="replace")).strip()

    async def devices(self) -> dict[str, str]:
        """serial -> state (device / offline / unauthorized / ...)."""
        res = await self.run("devices", timeout_s=10)
        result: dict[str, str] = {}
        for line in res.text.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                result[parts[0]] = parts[1]
        return result

    async def get_state(self, serial: str) -> str:
        return (await self.devices()).get(serial, "absent")

    async def push(self, serial: str, local: str, remote: str) -> None:
        (await self.run("push", local, remote, serial=serial, timeout_s=60)).check("adb push")
