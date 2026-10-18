"""Pre-shared token authentication for every WebSocket / API request, plus who is asking.

Through the Cloudflare Tunnel every request arrives from cloudflared on 127.0.0.1, but Cloudflare
always adds CF-Ray / CF-Connecting-IP, so "local" (a program on this Mac) can still be told apart.
"""
from __future__ import annotations

import hmac
import time

from aiohttp import web

from .config import AuthConfig


def extract_token(request: web.Request, allow_query: bool) -> str:
    header = request.headers.get("Authorization", "")
    if header:
        parts = header.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        return header.strip()  # xiaozhi adds "Bearer " itself only if the token has no space
    alt = request.headers.get("X-Shuttle-Token", "")
    if alt:
        return alt.strip()
    if allow_query:
        return request.query.get("token", "")
    return ""


def check_token(request: web.Request, cfg: AuthConfig) -> bool:
    given = extract_token(request, cfg.allow_query_token)
    if not given or not cfg.token:
        return False
    return hmac.compare_digest(given.encode(), cfg.token.encode())


def require_token(request: web.Request, cfg: AuthConfig) -> None:
    if not check_token(request, cfg):
        raise web.HTTPUnauthorized(text="invalid or missing token\n")


def via_tunnel(request: web.Request) -> bool:
    return "CF-Ray" in request.headers or "CF-Connecting-IP" in request.headers


def is_local(request: web.Request) -> bool:
    """A program on this machine (not the tunnel, not the LAN)."""
    return not via_tunnel(request) and request.remote in ("127.0.0.1", "::1")


def client_ip(request: web.Request) -> str:
    return request.headers.get("CF-Connecting-IP") or request.remote or "?"


class AuthGuard:
    """Blocks a client address for a while after repeated bad tokens. The token cannot be
    guessed; this stops scanners from hammering the server and flooding the log."""

    def __init__(self, max_failures: int = 20, window_s: float = 600.0, block_s: float = 900.0):
        self.max_failures, self.window_s, self.block_s = max_failures, window_s, block_s
        self._failures: dict[str, list[float]] = {}
        self._blocked: dict[str, float] = {}

    def blocked(self, ip: str) -> bool:
        until = self._blocked.get(ip)
        if until is None:
            return False
        if time.monotonic() >= until:
            del self._blocked[ip]
            return False
        return True

    def failure(self, ip: str) -> bool:
        """Record a bad token from ip; True when this one gets it blocked."""
        now = time.monotonic()
        if len(self._failures) > 10000:  # a flood from many addresses: start over
            self._failures.clear()
        recent = [t for t in self._failures.get(ip, []) if now - t < self.window_s]
        recent.append(now)
        if len(recent) >= self.max_failures:
            self._failures.pop(ip, None)
            self._blocked[ip] = now + self.block_s
            return True
        self._failures[ip] = recent
        return False
