"""Dashboard API endpoint tests (api.app) — TestClient, offline (fake providers via DI)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from argus.api.app import create_app
from argus.api.security import SessionConfig, hash_password
from argus.observability.health import CheckResult, HealthRegistry, HealthState
from argus.observability.status import SystemStatus

_STATUS = SystemStatus(
    version="0.1.0",
    env="dev",
    authorized_count=2,
    budget_total_usd=3.0,
    hit_correct=3,
    hit_total=4,
    degraded_sources=("BTC/USDT",),
)


def _health() -> HealthRegistry:
    reg = HealthRegistry()
    reg.register("self", lambda: CheckResult(name="self", state=HealthState.OK, detail="argus"))
    reg.register(
        "redis",
        lambda: CheckResult(name="redis", state=HealthState.DEGRADED, detail="not connected"),
    )
    return reg


def _open_client() -> TestClient:
    """App without auth (dev) — the user table is empty."""
    app = create_app(status_provider=lambda: _STATUS, health_provider=_health)
    return TestClient(app)


@pytest.fixture
def auth_client() -> Iterator[TestClient]:
    """Single-user app with auth required + a fixed session secret."""
    app = create_app(
        status_provider=lambda: _STATUS,
        health_provider=_health,
        users={"ada": hash_password("test-passphrase")},
        session=SessionConfig(secret=b"test-secret"),
        secure_cookie=False,  # TestClient uses http; a Secure cookie would not be sent
    )
    with TestClient(app) as client:
        yield client


# --- Auth disabled (dev) ---


def test_status_open_when_no_users() -> None:
    r = _open_client().get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == "0.1.0"
    assert body["hit_rate_pct"] == 75.0  # 3/4
    assert body["degraded_sources"] == ["BTC/USDT"]


def test_health_open_when_no_users() -> None:
    r = _open_client().get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "degraded"  # redis DEGRADED → overall degraded
    names = {c["name"] for c in body["checks"]}
    assert names == {"self", "redis"}


def test_login_disabled_when_no_users() -> None:
    r = _open_client().post("/api/login", json={"username": "x", "password": "y"})
    assert r.status_code == 400


# --- Auth required ---


def test_protected_requires_session(auth_client: TestClient) -> None:
    r = auth_client.get("/api/status")
    assert r.status_code == 401
    assert auth_client.get("/api/crypto/BTCUSDT").status_code == 401


def test_login_then_access(auth_client: TestClient) -> None:
    r = auth_client.post("/api/login", json={"username": "ada", "password": "test-passphrase"})
    assert r.status_code == 200
    assert r.json()["user"] == "ada"
    # The cookie is kept by the TestClient → the protected endpoint is now reachable.
    r2 = auth_client.get("/api/status")
    assert r2.status_code == 200
    assert r2.json()["version"] == "0.1.0"


def test_login_wrong_password(auth_client: TestClient) -> None:
    r = auth_client.post("/api/login", json={"username": "ada", "password": "wrong"})
    assert r.status_code == 401


def test_login_unknown_user(auth_client: TestClient) -> None:
    r = auth_client.post("/api/login", json={"username": "nobody", "password": "test-passphrase"})
    assert r.status_code == 401


def test_logout_clears_session(auth_client: TestClient) -> None:
    auth_client.post("/api/login", json={"username": "ada", "password": "test-passphrase"})
    assert auth_client.get("/api/status").status_code == 200
    auth_client.post("/api/logout")
    auth_client.cookies.clear()
    assert auth_client.get("/api/status").status_code == 401


def test_no_secret_in_responses(auth_client: TestClient) -> None:
    auth_client.post("/api/login", json={"username": "ada", "password": "test-passphrase"})
    for path in ("/api/status", "/api/health", "/api/crypto/BTCUSDT"):
        text = auth_client.get(path).text.lower()
        assert "test-passphrase" not in text
        assert "scrypt" not in text
        assert "secret" not in text


def test_login_sets_httponly_cookie(auth_client: TestClient) -> None:
    r = auth_client.post("/api/login", json={"username": "ada", "password": "test-passphrase"})
    set_cookie = r.headers.get("set-cookie", "")
    assert "argus_session=" in set_cookie
    assert "httponly" in set_cookie.lower()
    assert "samesite=strict" in set_cookie.lower()


# --- CORS (frontend on localhost:3000 in dev) ---


def test_cors_allows_configured_origin() -> None:
    app = create_app(
        status_provider=lambda: _STATUS,
        health_provider=_health,
        cors_origins=("http://localhost:3000",),
    )
    client = TestClient(app)
    r = client.get("/api/status", headers={"Origin": "http://localhost:3000"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"
    assert r.headers.get("access-control-allow-credentials") == "true"


def test_cors_disabled_by_default() -> None:
    # With no origin configured, no CORS header is returned (same-origin service).
    r = _open_client().get("/api/status", headers={"Origin": "http://localhost:3000"})
    assert "access-control-allow-origin" not in r.headers


def test_app_runs_supplied_lifespan_cleanup() -> None:
    events: list[str] = []

    @asynccontextmanager
    async def lifespan(_: object) -> AsyncIterator[None]:
        events.append("start")
        try:
            yield
        finally:
            events.append("stop")

    app = create_app(status_provider=lambda: _STATUS, lifespan=lifespan)
    with TestClient(app) as client:
        assert client.get("/api/status").status_code == 200
        assert events == ["start"]
    assert events == ["start", "stop"]
