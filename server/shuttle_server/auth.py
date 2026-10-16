"""Pre-shared token authentication for every WebSocket / API request."""
from __future__ import annotations

import hmac

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
