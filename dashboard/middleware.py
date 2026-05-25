"""HTTP middleware: HTTP Basic auth + in-memory per-IP rate limiting.

Both are off-by-default (auth via ``DASHBOARD_AUTH_ENABLED`` env var, rate
limiter is always on with a 60 req/min budget). Designed for a local /
single-tenant deployment, not a hardened public service.
"""

from __future__ import annotations

import base64
import os
import secrets
import time
from collections import defaultdict

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


# ── Auth config (read once at import) ────────────────────────────
AUTH_ENABLED = os.environ.get(
    "DASHBOARD_AUTH_ENABLED", ""
).lower() in ("1", "true", "yes")
AUTH_USERNAME = os.environ.get("DASHBOARD_USERNAME", "admin")
AUTH_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """HTTP Basic auth — skipped for /api/health and WebSocket upgrades."""

    async def dispatch(self, request: Request, call_next):
        if not AUTH_ENABLED:
            return await call_next(request)
        # Health endpoint must stay open for monitoring probes
        if request.url.path in ("/api/health", "/api/v1/health"):
            return await call_next(request)
        # WebSocket upgrades handle auth in the handler itself
        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8")
                user, passwd = decoded.split(":", 1)
                if (
                    secrets.compare_digest(user, AUTH_USERNAME)
                    and secrets.compare_digest(passwd, AUTH_PASSWORD)
                ):
                    return await call_next(request)
            except Exception:
                pass
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required"},
            headers={"WWW-Authenticate": 'Basic realm="Dashboard"'},
        )


# ── Rate limit (per IP, in-memory) ───────────────────────────────
_rate_store: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT = 60       # requests per minute per IP
RATE_WINDOW = 60.0    # seconds


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window per-IP limiter. Returns 429 over budget."""

    async def dispatch(self, request: Request, call_next):
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        cutoff = now - RATE_WINDOW
        _rate_store[client_ip] = [t for t in _rate_store[client_ip] if t > cutoff]
        if len(_rate_store[client_ip]) >= RATE_LIMIT:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Max 60 requests per minute."},
            )
        _rate_store[client_ip].append(now)
        return await call_next(request)
