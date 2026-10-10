"""Trading control API: PIT reads, auth/CSRF and fail-closed mutations."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from argus.api.app import CSRF_COOKIE, create_app
from argus.api.security import SessionConfig, hash_password
from argus.api.trading_models import MissionCreateRequest
from argus.config.models import RetentionConfig, RuntimeConfig
from argus.core.trading import MissionStatus
from argus.interface.trading_control import TradingControlService
from argus.storage.sqlite import SqliteStore


@pytest.fixture
def trading_client() -> Iterator[tuple[TestClient, SqliteStore]]:
    store = SqliteStore()
    config = RuntimeConfig(retention=RetentionConfig(enabled=True))
    control = TradingControlService(store, lambda: config, owner_id="api-test-owner")
    app = create_app(
        trading_control_provider=lambda: control,
        users={"ada": hash_password("test-passphrase")},
        session=SessionConfig(secret=b"trading-api-test"),
        secure_cookie=False,
        trading_local_control=True,
    )
    with TestClient(app) as client:
        yield client, store
    store.close()


def _login(client: TestClient) -> str:
    response = client.post("/api/login", json={"username": "ada", "password": "test-passphrase"})
    assert response.status_code == 200
    csrf = client.cookies.get(CSRF_COOKIE)
    assert csrf
    return csrf


def test_trading_reads_are_stored_and_honestly_no_data(
    trading_client: tuple[TestClient, SqliteStore],
) -> None:
    client, _store = trading_client
    _login(client)
    status = client.get("/api/trading/status")
    account = client.get("/api/trading/account")
    performance = client.get("/api/trading/performance")
    news = client.get("/api/trading/news")
    plans = client.get("/api/trading/plans")
    health = client.get("/api/trading/health")
    assert status.status_code == 200
    assert status.json()["mode"] == "public_monitor"
    assert status.json()["spot_execution_available"] is False
    assert status.json()["live_execution_enabled"] is False
    assert account.json()["status"] == "no_data"
    assert account.json()["snapshot"] is None
    assert performance.json()["data_status"] == "pending"
    assert performance.json()["net_pnl"] is None
    assert news.json()["records"] == []
    assert news.json()["data_status"] == "pending"
    assert plans.json()["plans"] == []
    assert health.json()["execution"] == "disabled"
    assert health.json()["private_account"] == "no_data"


def test_trading_health_rejects_future_market_timestamp(
    trading_client: tuple[TestClient, SqliteStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, store = trading_client
    _login(client)
    future = datetime.now(UTC) + timedelta(minutes=5)
    monkeypatch.setattr(store, "latest_market_timestamp", lambda: future)
    response = client.get("/api/trading/health")
    assert response.status_code == 200
    assert response.json()["public_market_data"] == "pending"
    assert "stale" in " ".join(response.json()["reasons"])


def test_universe_endpoint_uses_bounded_cursor_page(
    trading_client: tuple[TestClient, SqliteStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, store = trading_client
    _login(client)
    observed: dict[str, object] = {}

    def page(*, limit: int, cursor: str | None) -> tuple[list[object], int, int, None, str]:
        observed.update(limit=limit, cursor=cursor)
        return [], 590, 12, None, "BTCUSDT"

    monkeypatch.setattr(store, "latest_universe_snapshot_page", page)
    response = client.get("/api/trading/universe?limit=25&cursor=ADAUSDT")

    assert response.status_code == 200
    assert response.json()["scanned_count"] == 590
    assert response.json()["returned_count"] == 0
    assert response.json()["next_cursor"] == "BTCUSDT"
    assert observed == {"limit": 25, "cursor": "ADAUSDT"}
    assert client.get("/api/trading/universe?limit=501").status_code == 422


def test_trading_mutation_requires_auth_and_csrf(
    trading_client: tuple[TestClient, SqliteStore],
) -> None:
    client, _store = trading_client
    payload = {"capital_limit": "1000", "duration_days": 7, "mode": "public_monitor"}
    assert client.post("/api/trading/missions", json=payload).status_code == 401
    csrf = _login(client)
    assert client.post("/api/trading/missions", json=payload).status_code == 403
    assert (
        client.post(
            "/api/trading/missions", json=payload, headers={"X-Argus-CSRF": "wrong"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/trading/missions", json=payload, headers={"X-Argus-CSRF": csrf}
        ).status_code
        == 200
    )


def test_public_mission_is_disarmed_then_arms_without_execution_capability(
    trading_client: tuple[TestClient, SqliteStore],
) -> None:
    client, store = trading_client
    csrf = _login(client)
    created = client.post(
        "/api/trading/missions",
        json={"capital_limit": "250", "duration_days": 7, "mode": "public_monitor"},
        headers={"X-Argus-CSRF": csrf},
    )
    assert created.status_code == 200
    mission = created.json()["mission"]
    assert mission["status"] == "disarmed"
    armed = client.post(
        f"/api/trading/missions/{mission['mission_id']}/arm",
        json={"confirm": True, "expected_revision": mission["revision"]},
        headers={"X-Argus-CSRF": csrf},
    )
    assert armed.status_code == 200
    assert armed.json()["mission"]["status"] == "running"
    assert "produces no order intents/submissions" in armed.json()["message"]
    assert store.order_intents(mission["mission_id"]) == []
    orders = client.get("/api/trading/orders").json()
    assert orders["exchange_orders"] == []
    assert orders["data_status"] == "pending"


def test_live_mode_cannot_be_created(
    trading_client: tuple[TestClient, SqliteStore],
) -> None:
    client, _store = trading_client
    csrf = _login(client)
    response = client.post(
        "/api/trading/missions",
        json={"capital_limit": "100", "duration_days": 1, "mode": "live_spot"},
        headers={"X-Argus-CSRF": csrf},
    )
    assert response.status_code == 409
    assert "mode" in response.json()["detail"]


def test_try_reference_requires_consistent_current_conversion(
    trading_client: tuple[TestClient, SqliteStore],
) -> None:
    client, _store = trading_client
    csrf = _login(client)
    inconsistent = client.post(
        "/api/trading/missions",
        json={
            "capital_limit_usdt": "250",
            "input_budget_try": "10000",
            "try_per_usdt": "30",
            "conversion_rate_source": "verified-public-fx",
            "conversion_rate_at": "2026-09-02T12:00:00Z",
        },
        headers={"X-Argus-CSRF": csrf},
    )
    assert inconsistent.status_code == 422


def test_kill_switch_is_one_way_and_halts_active_public_mission(
    trading_client: tuple[TestClient, SqliteStore],
) -> None:
    client, _store = trading_client
    csrf = _login(client)
    created = client.post(
        "/api/trading/missions",
        json={"capital_limit": "100", "duration_days": 1},
        headers={"X-Argus-CSRF": csrf},
    ).json()["mission"]
    client.post(
        f"/api/trading/missions/{created['mission_id']}/arm",
        json={"confirm": True, "expected_revision": created["revision"]},
        headers={"X-Argus-CSRF": csrf},
    )
    killed = client.post(
        "/api/trading/kill-switch",
        json={"enabled": True, "reason": "operator emergency halt"},
        headers={"X-Argus-CSRF": csrf},
    )
    assert killed.status_code == 200
    assert killed.json()["enabled"] is True
    assert created["mission_id"] in killed.json()["halted_mission_ids"]
    assert client.get("/api/trading/status").json()["kill_switch"] is True
    again = client.post(
        "/api/trading/missions",
        json={"capital_limit": "100", "duration_days": 1},
        headers={"X-Argus-CSRF": csrf},
    )
    assert again.status_code == 409


def test_trading_mutation_is_closed_without_configured_auth() -> None:
    store = SqliteStore()
    try:
        control = TradingControlService(store, RuntimeConfig, owner_id="dev")
        app = create_app(
            trading_control_provider=lambda: control,
            trading_local_control=True,
            secure_cookie=False,
        )
        with TestClient(app) as client:
            response = client.post(
                "/api/trading/missions",
                json={"capital_limit": "100", "duration_days": 1},
            )
        assert response.status_code == 403
    finally:
        store.close()


def test_trading_mutation_is_closed_on_non_local_control_bind(
    trading_client: tuple[TestClient, SqliteStore],
) -> None:
    _client, store = trading_client
    control = TradingControlService(store, RuntimeConfig, owner_id="remote")
    app = create_app(
        trading_control_provider=lambda: control,
        users={"ada": hash_password("test-passphrase")},
        session=SessionConfig(secret=b"remote-test"),
        secure_cookie=False,
        trading_local_control=False,
    )
    with TestClient(app) as client:
        csrf = _login(client)
        response = client.post(
            "/api/trading/missions",
            json={"capital_limit": "100", "duration_days": 1},
            headers={"X-Argus-CSRF": csrf},
        )
    assert response.status_code == 403
    assert "local-control" in response.json()["detail"]


def test_restart_moves_running_mission_back_to_arming_for_reconciliation() -> None:
    store = SqliteStore()
    config = RuntimeConfig(retention=RetentionConfig(enabled=True))
    try:
        first = TradingControlService(store, lambda: config, owner_id="first-process")
        created = first.create_mission(
            MissionCreateRequest(capital_limit="100", duration_days=1), actor="operator"
        ).mission
        running = first.arm_mission(
            created.mission_id,
            expected_revision=created.revision,
            actor="operator",
        ).mission
        assert running.status is MissionStatus.RUNNING

        restarted = TradingControlService(store, lambda: config, owner_id="new-process")
        assert restarted.recover_after_restart() == (created.mission_id,)
        recovered = store.load_trading_mission(created.mission_id)
        assert recovered is not None
        assert recovered.status is MissionStatus.ARMING
        assert recovered.revision == running.revision + 1
        assert store.order_intents(created.mission_id) == []
    finally:
        store.close()
