"""Auth middleware behavior: loopback bypass, remote 401s, lockout,
empty-password refusal, and local-only admin endpoints."""

from __future__ import annotations

import base64

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import dashboard.middleware as mw
from dashboard.middleware import BasicAuthMiddleware
from dashboard.routes import admin as admin_routes

REMOTE_IP = "100.101.102.103"  # e.g. a Tailscale peer
LOOPBACK_IP = "127.0.0.1"


class ForceClientAddr:
    """ASGI wrapper pinning scope['client'] to a chosen address."""

    def __init__(self, app, host: str):
        self.app = app
        self.host = host

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope = dict(scope)
            scope["client"] = (self.host, 12345) if self.host is not None else None
        await self.app(scope, receive, send)


def make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(BasicAuthMiddleware)
    app.include_router(admin_routes.router)

    @app.get("/api/thing")
    async def read_thing():
        return {"ok": True}

    @app.post("/api/thing")
    async def write_thing():
        return {"ok": True}

    return app


def client_for(app, host: str) -> TestClient:
    return TestClient(ForceClientAddr(app, host))


def basic(user: str, password: str) -> dict:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture()
def auth_on(monkeypatch):
    monkeypatch.setattr(mw, "AUTH_ENABLED", True)
    monkeypatch.setattr(mw, "AUTH_USERNAME", "marc")
    monkeypatch.setattr(mw, "AUTH_PASSWORD", "correct-horse")
    mw._auth_failures.clear()
    yield
    mw._auth_failures.clear()


def test_auth_disabled_passes_remote(monkeypatch):
    monkeypatch.setattr(mw, "AUTH_ENABLED", False)
    c = client_for(make_app(), REMOTE_IP)
    assert c.get("/api/thing").status_code == 200
    assert c.post("/api/thing").status_code == 200


def test_remote_requires_credentials(auth_on):
    c = client_for(make_app(), REMOTE_IP)
    r = c.post("/api/thing")
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers


def test_remote_with_correct_credentials(auth_on):
    c = client_for(make_app(), REMOTE_IP)
    assert c.post("/api/thing", headers=basic("marc", "correct-horse")).status_code == 200


def test_remote_with_wrong_password(auth_on):
    c = client_for(make_app(), REMOTE_IP)
    assert c.post("/api/thing", headers=basic("marc", "wrong")).status_code == 401


def test_loopback_bypasses_auth(auth_on):
    c = client_for(make_app(), LOOPBACK_IP)
    assert c.get("/api/thing").status_code == 200
    assert c.post("/api/thing").status_code == 200


def test_health_stays_open_for_remote(auth_on):
    app = make_app()

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    # Middleware added before route definition still wraps it.
    c = client_for(app, REMOTE_IP)
    assert c.get("/api/health").status_code == 200


def test_lockout_after_repeated_failures(auth_on):
    c = client_for(make_app(), REMOTE_IP)
    for _ in range(mw.MAX_AUTH_FAILURES):
        assert c.post("/api/thing", headers=basic("marc", "bad")).status_code == 401
    # Even the correct password is refused while locked out
    r = c.post("/api/thing", headers=basic("marc", "correct-horse"))
    assert r.status_code == 429


def test_missing_header_does_not_count_toward_lockout(auth_on):
    c = client_for(make_app(), REMOTE_IP)
    for _ in range(mw.MAX_AUTH_FAILURES + 2):
        assert c.post("/api/thing").status_code == 401  # never escalates to 429
    assert c.post("/api/thing", headers=basic("marc", "correct-horse")).status_code == 200


def test_empty_password_refused(monkeypatch):
    monkeypatch.setattr(mw, "AUTH_ENABLED", True)
    monkeypatch.setattr(mw, "AUTH_PASSWORD", "")
    with pytest.raises(RuntimeError, match="DASHBOARD_PASSWORD"):
        make_app()  # middleware __init__ runs at app construction
        client_for(make_app(), REMOTE_IP).get("/api/thing")


def test_unknown_client_requires_auth(auth_on):
    """request.client=None (some proxy setups) must NOT count as loopback."""
    c = client_for(make_app(), None)
    assert c.post("/api/thing").status_code == 401
    assert c.post("/api/thing", headers=basic("marc", "correct-horse")).status_code == 200


def test_admin_quit_local_only(auth_on):
    c = client_for(make_app(), REMOTE_IP)
    r = c.post("/api/admin/quit", headers=basic("marc", "correct-horse"))
    assert r.status_code == 403  # authenticated but remote -> still refused


def test_admin_open_folder_local_only(auth_on):
    c = client_for(make_app(), REMOTE_IP)
    r = c.post("/api/admin/open-folder?which=logs", headers=basic("marc", "correct-horse"))
    assert r.status_code == 403
