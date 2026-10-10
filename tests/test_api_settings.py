"""Settings page endpoint tests (api.app settings) — offline, fake writer/reader via DI.

Critical: secrets NEVER leak (GET returns only {set,last4}); write endpoints require auth;
env_writer is called with the right keys (None = leave untouched); an invalid config write is
rejected (the old one is kept).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from argus.api.app import create_app
from argus.api.security import SessionConfig, hash_password
from argus.api.settings_models import (
    LLMSettingsView,
    RoleModel,
    SecretStatus,
    SettingsView,
    TelegramSettingsView,
    TriggerSettingsView,
    WatchlistSettingsView,
)
from argus.config.models import AlertConfig, RuntimeConfig, TriggerThresholds, WatchlistItem
from argus.core.enums import AssetClass

_SECRET = "not-a-real-key-for-tests-AB12"


def _view() -> SettingsView:
    return SettingsView(
        llm=LLMSettingsView(
            anthropic_api_key=SecretStatus.from_secret(SecretStr(_SECRET)),
            openai_api_key=SecretStatus.from_secret(None),
            openrouter_api_key=SecretStatus.from_secret(None),
            llm_provider="claude",
            providers=["claude", "ollama", "openai", "openrouter"],
            live_providers=["claude", "ollama"],
            ollama_base_url="http://localhost:11434",
            ollama_model="llama3.2",
            model_routine="claude-sonnet-4-6",
            model_deep="claude-opus-4-8",
            model_copilot="claude-sonnet-4-6",
            llm_daily_budget_usd=3.0,
            roles=[
                RoleModel(
                    role="routine",
                    label="Routine",
                    provider="claude",
                    model="claude-sonnet-4-6",
                    active=True,
                ),
                RoleModel(
                    role="deep",
                    label="Deep",
                    provider="ollama",
                    model="qwen2.5",
                    active=True,
                ),
                RoleModel(
                    role="copilot",
                    label="Copilot",
                    provider="claude",
                    model="claude-sonnet-4-6",
                    active=True,
                ),
                RoleModel(
                    role="news_router",
                    label="News router",
                    provider="claude",
                    model="claude-sonnet-4-6",
                    active=True,
                ),
                RoleModel(
                    role="manager", label="Manager AI", provider="—", model="—", active=False
                ),
            ],
        ),
        telegram=TelegramSettingsView(
            bot_token=SecretStatus.from_secret(None),
            authorized_chat_ids=[111, 222],
        ),
        watch=WatchlistSettingsView(
            watchlist=[WatchlistItem(symbol="BTCUSDT", asset_class=AssetClass.CRYPTO)],
            holdings=[],
        ),
        triggers=TriggerSettingsView(triggers=TriggerThresholds(), alerts=AlertConfig()),
    )


class Recorder:
    """Captures env_writer/config_writer calls (fake)."""

    def __init__(self) -> None:
        self.env_updates: dict[str, str] | None = None
        self.saved_config: RuntimeConfig | None = None

    def env_writer(self, updates: dict[str, str]) -> None:
        self.env_updates = updates

    def config_writer(self, config: RuntimeConfig) -> None:
        self.saved_config = config


@pytest.fixture
def rec() -> Recorder:
    return Recorder()


def _client(rec: Recorder, *, auth: bool = False) -> TestClient:
    kwargs = {}
    if auth:
        kwargs = {
            "users": {"ada": hash_password("test-passphrase")},
            "session": SessionConfig(secret=b"test-secret"),
            "secure_cookie": False,
        }
    app = create_app(
        settings_reader=_view,
        env_writer=rec.env_writer,
        config_writer=rec.config_writer,
        config_reader=lambda: RuntimeConfig(),
        **kwargs,  # type: ignore[arg-type]
    )
    return TestClient(app)


@pytest.fixture
def client(rec: Recorder) -> TestClient:
    return _client(rec)


@pytest.fixture
def auth_client(rec: Recorder) -> Iterator[TestClient]:
    c = _client(rec, auth=True)
    with c:
        yield c


# ── GET /api/settings — masking ───────────────────────────────────────────────


def test_get_settings_masks_secret(client: TestClient) -> None:
    r = client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["llm"]["anthropic_api_key"] == {"set": True, "last4": "AB12"}
    # The raw key is NOT in the response.
    assert "not-a-real-key" not in r.text
    assert _SECRET not in r.text


def test_get_settings_unset_secret(client: TestClient) -> None:
    body = client.get("/api/settings").json()
    assert body["telegram"]["bot_token"] == {"set": False, "last4": None}


def test_get_settings_exposes_nonsecret(client: TestClient) -> None:
    body = client.get("/api/settings").json()
    assert body["llm"]["model_deep"] == "claude-opus-4-8"
    assert body["llm"]["model_copilot"] == "claude-sonnet-4-6"
    assert body["llm"]["llm_provider"] == "claude"
    assert body["telegram"]["authorized_chat_ids"] == [111, 222]
    assert body["watch"]["watchlist"][0]["symbol"] == "BTCUSDT"


def test_get_settings_lists_roles(client: TestClient) -> None:
    # Models page role→model table: active roles (routine/deep/copilot) + planned roles.
    body = client.get("/api/settings").json()["llm"]
    by_role = {r["role"]: r for r in body["roles"]}
    assert by_role["routine"]["active"] is True
    assert by_role["deep"]["active"] is True
    assert by_role["copilot"]["active"] is True
    assert by_role["news_router"]["active"] is True  # active
    # Every active role carries its own provider (role→model table column).
    assert by_role["routine"]["provider"] == "claude"
    assert by_role["deep"]["provider"] == "ollama"  # a different provider per role
    # Planned roles (multi-AI orchestration — not used yet) show as inactive.
    assert by_role["manager"]["active"] is False
    # UI dropdown list + working providers.
    assert set(body["providers"]) == {"claude", "ollama", "openai", "openrouter"}
    assert set(body["live_providers"]) == {"claude", "ollama"}


def test_get_settings_masks_extra_provider_keys(client: TestClient) -> None:
    # OpenAI/OpenRouter keys are masked too (not set → set:false).
    llm = client.get("/api/settings").json()["llm"]
    assert llm["openai_api_key"] == {"set": False, "last4": None}
    assert llm["openrouter_api_key"] == {"set": False, "last4": None}


# ── POST /api/settings/llm ────────────────────────────────────────────────────


def test_post_llm_writes_env(client: TestClient, rec: Recorder) -> None:
    r = client.post(
        "/api/settings/llm",
        json={
            "anthropic_api_key": "new-test-key",
            "model_deep": "claude-opus-4-8",
            "llm_daily_budget_usd": 5.0,
        },
    )
    assert r.status_code == 200
    assert r.json()["requires_restart"] is True
    assert rec.env_updates == {
        "ANTHROPIC_API_KEY": "new-test-key",
        "ARGUS_MODEL_DEEP": "claude-opus-4-8",
        "ARGUS_LLM_DAILY_BUDGET_USD": "5.0",
    }


def test_post_llm_writes_copilot_model(client: TestClient, rec: Recorder) -> None:
    client.post("/api/settings/llm", json={"model_copilot": "claude-haiku-4-5"})
    assert rec.env_updates == {"ARGUS_MODEL_COPILOT": "claude-haiku-4-5"}


def test_post_llm_writes_per_role_provider(client: TestClient, rec: Recorder) -> None:
    # Per-role provider + model are written to .env in a single POST.
    client.post(
        "/api/settings/llm",
        json={"provider_deep": "ollama", "model_deep": "qwen2.5"},
    )
    assert rec.env_updates == {
        "ARGUS_PROVIDER_DEEP": "ollama",
        "ARGUS_MODEL_DEEP": "qwen2.5",
    }


def test_post_llm_writes_news_router_role(client: TestClient, rec: Recorder) -> None:
    # News-router role — provider + model are written to .env in a single POST.
    client.post(
        "/api/settings/llm",
        json={"provider_news_router": "ollama", "model_news_router": "qwen2.5"},
    )
    assert rec.env_updates == {
        "ARGUS_PROVIDER_NEWS_ROUTER": "ollama",
        "ARGUS_MODEL_NEWS_ROUTER": "qwen2.5",
    }


def test_post_llm_writes_extra_provider_keys(client: TestClient, rec: Recorder) -> None:
    client.post(
        "/api/settings/llm",
        json={"openai_api_key": "openai-test-key", "openrouter_api_key": "openrouter-test-key"},
    )
    assert rec.env_updates == {
        "ARGUS_OPENAI_API_KEY": "openai-test-key",
        "ARGUS_OPENROUTER_API_KEY": "openrouter-test-key",
    }


def test_post_llm_pending_provider_note(client: TestClient) -> None:
    # Picking a not-yet-working provider for an active role returns an honest "soon" note.
    r = client.post("/api/settings/llm", json={"provider_deep": "openai"})
    assert r.status_code == 200
    assert "openai" in r.json()["message"]
    assert "not active yet (coming soon)" in r.json()["message"]


def test_post_llm_none_skips_key(client: TestClient, rec: Recorder) -> None:
    # anthropic_api_key not sent → ANTHROPIC_API_KEY in .env is left untouched (write-only).
    client.post("/api/settings/llm", json={"model_routine": "claude-sonnet-4-6"})
    assert rec.env_updates == {"ARGUS_MODEL_ROUTINE": "claude-sonnet-4-6"}
    assert "ANTHROPIC_API_KEY" not in (rec.env_updates or {})


def test_post_llm_provider_ollama(client: TestClient, rec: Recorder) -> None:
    client.post(
        "/api/settings/llm",
        json={"llm_provider": "ollama", "ollama_base_url": "http://ollama-host:11434"},
    )
    assert rec.env_updates == {
        "ARGUS_LLM_PROVIDER": "ollama",
        "ARGUS_OLLAMA_BASE_URL": "http://ollama-host:11434",
    }


# ── _default_settings_reader role mapping (derived logic) ─────────────────────


def test_reader_roles_claude_use_role_models(monkeypatch: pytest.MonkeyPatch) -> None:
    # With the Claude provider, active roles show their own provider + models.
    from argus.api import app as app_mod
    from argus.config.settings import Settings

    s = Settings(llm_provider="claude", model_routine="m-r", model_deep="m-d", model_copilot="m-c")
    monkeypatch.setattr("argus.config.settings.get_settings", lambda: s)
    by_role = {r.role: r for r in app_mod._default_settings_reader().llm.roles}
    assert by_role["routine"].model == "m-r" and by_role["routine"].provider == "claude"
    assert by_role["deep"].model == "m-d" and by_role["deep"].provider == "claude"
    assert by_role["copilot"].model == "m-c"
    # news_router is active — it shows its own provider/model.
    assert by_role["news_router"].active is True
    assert by_role["news_router"].provider == "claude"


def test_reader_roles_per_role_provider_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    # Independent provider + model per role — NO collapsing to a single model.
    from argus.api import app as app_mod
    from argus.config.settings import Settings

    s = Settings(
        provider_routine="claude",
        model_routine="claude-sonnet-4-6",
        provider_deep="ollama",
        model_deep="qwen2.5",
        provider_copilot="ollama",
        model_copilot="phi3",
    )
    monkeypatch.setattr("argus.config.settings.get_settings", lambda: s)
    by_role = {r.role: r for r in app_mod._default_settings_reader().llm.roles}
    assert by_role["routine"].provider == "claude"
    assert by_role["deep"].provider == "ollama" and by_role["deep"].model == "qwen2.5"
    assert by_role["copilot"].provider == "ollama" and by_role["copilot"].model == "phi3"
    # Planned roles are still inactive + "—".
    assert by_role["manager"].active is False
    assert by_role["manager"].provider == "—" and by_role["manager"].model == "—"


# ── POST /api/settings/telegram ───────────────────────────────────────────────


def test_post_telegram_writes_env(client: TestClient, rec: Recorder) -> None:
    client.post(
        "/api/settings/telegram",
        json={"bot_token": "123:ABC", "authorized_chat_ids": [111, 222]},
    )
    assert rec.env_updates == {
        "TELEGRAM_BOT_TOKEN": "123:ABC",
        "ARGUS_AUTHORIZED_CHAT_IDS": "111,222",
    }


# ── POST /api/settings/data (data source keys) ───────────────────────────────


def test_post_data_keys_writes_env(client: TestClient, rec: Recorder) -> None:
    r = client.post(
        "/api/settings/data",
        json={"evds_key": "EVDS123", "finnhub_key": "FH456"},  # cryptopanic None = untouched
    )
    assert r.status_code == 200
    assert r.json()["requires_restart"] is True
    assert rec.env_updates == {
        "ARGUS_EVDS_KEY": "EVDS123",
        "ARGUS_FINNHUB_KEY": "FH456",
    }


def test_get_settings_includes_data_section(client: TestClient) -> None:
    data = client.get("/api/settings").json()["data"]
    # _view() gave no data → default: all unset (honest status, no secrets).
    assert data["evds_key"] == {"set": False, "last4": None}
    assert data["cryptopanic_token"]["set"] is False
    assert data["finnhub_key"]["set"] is False


# ── POST /api/settings/watchlist ──────────────────────────────────────────────


def test_post_watchlist_writes_config(client: TestClient, rec: Recorder) -> None:
    r = client.post(
        "/api/settings/watchlist",
        json={
            "watchlist": [{"symbol": "ETHUSDT", "asset_class": "crypto", "tier": "watchlist"}],
            "holdings": [],
        },
    )
    assert r.status_code == 200
    assert r.json()["requires_restart"] is True
    assert "restart the watch process" in r.json()["message"]
    assert rec.saved_config is not None
    assert rec.saved_config.watchlist[0].symbol == "ETHUSDT"


def test_post_invalid_watchlist_rejected(client: TestClient, rec: Recorder) -> None:
    r = client.post(
        "/api/settings/watchlist",
        json={"watchlist": [{"symbol": "", "asset_class": "crypto"}], "holdings": []},
    )
    assert r.status_code == 422  # empty symbol → rejected by pydantic
    assert rec.saved_config is None  # config is not written (the old one is kept)


# ── POST /api/settings/triggers ───────────────────────────────────────────────


def test_post_triggers_writes_config(client: TestClient, rec: Recorder) -> None:
    r = client.post(
        "/api/settings/triggers",
        json={
            "triggers": {"rsi_overbought": 75.0},
            "alerts": {"min_instant_severity": "high"},
        },
    )
    assert r.status_code == 200
    assert r.json()["requires_restart"] is True
    assert rec.saved_config is not None
    assert rec.saved_config.triggers.rsi_overbought == 75.0


# ── Auth + secret leakage ─────────────────────────────────────────────────────


def test_settings_endpoints_require_auth(auth_client: TestClient) -> None:
    assert auth_client.get("/api/settings").status_code == 401
    assert auth_client.post("/api/settings/llm", json={}).status_code == 401
    assert auth_client.post("/api/settings/telegram", json={}).status_code == 401
    assert auth_client.post("/api/settings/data", json={}).status_code == 401
    assert (
        auth_client.post(
            "/api/settings/watchlist", json={"watchlist": [], "holdings": []}
        ).status_code
        == 401
    )


def test_no_secret_leak_in_get(client: TestClient) -> None:
    text = client.get("/api/settings").text
    assert "not-a-real-key" not in text
    assert "get_secret_value" not in text
    assert _SECRET not in text
