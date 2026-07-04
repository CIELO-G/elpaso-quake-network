"""HTTP middleware: HTTP Basic auth + in-memory per-IP rate limiting.

Auth is off-by-default (``DASHBOARD_AUTH_ENABLED`` env var); the rate
limiter is always on. Loopback clients (the local pywebview window) bypass
auth entirely — the password only gates remote clients (LAN / Tailscale).
Designed for a small trusted deployment, not a hardened public service.
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
import time
from collections import defaultdict

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("dashboard.auth")

# ── Auth config (read once at import) ────────────────────────────
AUTH_ENABLED = os.environ.get("DASHBOARD_AUTH_ENABLED", "").lower() in ("1", "true", "yes")
AUTH_USERNAME = os.environ.get("DASHBOARD_USERNAME", "admin")
AUTH_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

# Client hosts treated as the local machine (pywebview window / same box).
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")

# Brute-force lockout: after MAX_AUTH_FAILURES failed attempts within
# AUTH_FAIL_WINDOW seconds, an IP gets 429s until the window drains.
MAX_AUTH_FAILURES = 5
AUTH_FAIL_WINDOW = 900.0  # 15 minutes
_auth_failures: dict[str, list[float]] = defaultdict(list)


def is_loopback(request: Request) -> bool:
    return request.client is not None and request.client.host in LOOPBACK_HOSTS


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """HTTP Basic auth for non-loopback clients.

    Skipped for: loopback clients (local window), /api/health (monitoring
    probes), and WebSocket upgrades (status-only stream; auth handled at
    the HTTP layer for everything that matters).

    Refuses to start with auth enabled and an empty password — that
    configuration would accept any login.
    """

    def __init__(self, app):
        super().__init__(app)
        if AUTH_ENABLED and not AUTH_PASSWORD:
            raise RuntimeError(
                "DASHBOARD_AUTH_ENABLED is set but DASHBOARD_PASSWORD is empty. "
                "Set a password: export DASHBOARD_PASSWORD='...'"
            )

    async def dispatch(self, request: Request, call_next):
        if not AUTH_ENABLED:
            return await call_next(request)
        # Local window / same machine: no password.
        if is_loopback(request):
            return await call_next(request)
        # WebSocket upgrades handle auth in the handler itself
        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)
        # Health endpoint must stay open for monitoring probes
        if request.url.path in ("/api/health", "/api/v1/health"):
            return await call_next(request)

        ip = request.client.host if request.client else "unknown"
        now = time.time()
        recent = [t for t in _auth_failures.get(ip, ()) if t > now - AUTH_FAIL_WINDOW]
        if recent:
            _auth_failures[ip] = recent
        else:
            _auth_failures.pop(ip, None)  # keep the dict from accumulating stale IPs
        if len(recent) >= MAX_AUTH_FAILURES:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many failed login attempts. Try again later."},
            )

        auth = request.headers.get("authorization", "")
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8")
                user, passwd = decoded.split(":", 1)
                if secrets.compare_digest(user, AUTH_USERNAME) and secrets.compare_digest(
                    passwd, AUTH_PASSWORD
                ):
                    _auth_failures.pop(ip, None)
                    return await call_next(request)
            except Exception:
                pass
            # A malformed or wrong Authorization header is a failed attempt;
            # a missing one is just the browser's first visit (no log spam).
            _auth_failures[ip].append(now)
            logger.warning("Failed dashboard login from %s (%d/%d in window)",
                           ip, len(_auth_failures[ip]), MAX_AUTH_FAILURES)
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required"},
            headers={"WWW-Authenticate": 'Basic realm="Dashboard"'},
        )


# ── Rate limit (per IP, in-memory) ───────────────────────────────
# Only "action" endpoints count toward the budget. Predictable polling
# loops and cheap browsing reads are exempt — they're bounded by their
# setInterval cadence and don't represent an abuse vector. What we still
# want to catch: runaway POSTs (save/relocate/admin) or someone scraping
# waveforms in a tight loop.
_rate_store: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT = 600  # requests per minute per IP (counted endpoints only)
RATE_WINDOW = 60.0  # seconds

# Prefixes of paths exempt from the limit. Matched with startswith().
# Includes dashboard polling endpoints + catalog browsing + static assets.
RATE_EXEMPT_PREFIXES: tuple[str, ...] = (
    # Predictable polling (fire on setInterval — bounded by design)
    "/api/status",
    "/api/stats",
    "/api/progress",
    "/api/throughput",
    "/api/disk",
    "/api/errors",
    "/api/station_health",
    "/api/data_completeness",
    "/api/health",
    "/api/pipeline/running",
    # User browsing — cheap CSV reads + chart endpoints
    "/api/catalog",
    "/api/stations",
    "/api/event_rate",
    "/api/magnitude_frequency",
    "/api/depth_distribution",
    "/api/pick_quality",
    "/api/station_picks",
    "/api/faults",
    # Static assets and the main page
    "/static/",
    "/favicon",
)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window per-IP limiter. Returns 429 over budget.

    Skips exempt paths (polling, browsing, static) and WebSockets.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # Exempt root (the index.html page) and exempt-prefixes
        if path == "/" or path.startswith(RATE_EXEMPT_PREFIXES):
            return await call_next(request)
        # WebSockets are long-lived; rate-limiting the upgrade is meaningless
        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        cutoff = now - RATE_WINDOW
        _rate_store[client_ip] = [t for t in _rate_store[client_ip] if t > cutoff]
        if len(_rate_store[client_ip]) >= RATE_LIMIT:
            return JSONResponse(
                status_code=429,
                content={
                    "detail": f"Rate limit exceeded. Max {RATE_LIMIT} action requests per minute."
                },
            )
        _rate_store[client_ip].append(now)
        return await call_next(request)
