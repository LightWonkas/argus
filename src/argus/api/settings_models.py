"""Settings page endpoint contracts — secret masking + view/update models.

**Critical:** in GET views a secret field is only a :class:`SecretStatus` (set + last 4
characters); the raw key/token never enters any response. In POST updates a secret field is
``str | None`` — ``None`` = "leave untouched" (write-only behaviour), a value = write to
``.env``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from argus.config.models import AlertConfig, HoldingItem, TriggerThresholds, WatchlistItem


class SecretStatus(BaseModel):
    """**Masked** state of a secret — only whether it is set + last 4 chars (NO raw value)."""

    model_config = ConfigDict(frozen=True)

    set: bool
    last4: str | None = None

    @classmethod
    def from_secret(cls, value: SecretStr | None) -> SecretStatus:
        if value is None:
            return cls(set=False)
        raw = value.get_secret_value()
        if not raw:
            return cls(set=False)
        return cls(set=True, last4=raw[-4:])


# ── GET /api/settings (masked read) ──────────────────────────────────────────


class RoleModel(BaseModel):
    """An AI role → its assigned provider + model (Models page table)."""

    model_config = ConfigDict(frozen=True)

    role: str  # routine | deep | copilot | (planned: news_router/exchange_expert/manager)
    label: str  # display name
    provider: str  # claude | ollama | openai | openrouter | "—" (planned)
    model: str  # assigned model name
    active: bool = Field(
        ..., description="Whether the system currently uses it (planned roles are False)"
    )


class LLMSettingsView(BaseModel):
    model_config = ConfigDict(frozen=True)

    anthropic_api_key: SecretStatus
    openai_api_key: SecretStatus
    openrouter_api_key: SecretStatus
    llm_provider: Literal["claude", "ollama"]
    providers: list[str] = Field(default_factory=list)  # UI dropdown — supported providers
    live_providers: list[str] = Field(default_factory=list)  # working at runtime (claude/ollama)
    ollama_base_url: str
    ollama_model: str
    model_routine: str
    model_deep: str
    model_copilot: str
    llm_daily_budget_usd: float
    roles: list[RoleModel] = Field(default_factory=list)


class TelegramSettingsView(BaseModel):
    model_config = ConfigDict(frozen=True)

    bot_token: SecretStatus
    authorized_chat_ids: list[int]


class WatchlistSettingsView(BaseModel):
    model_config = ConfigDict(frozen=True)

    watchlist: list[WatchlistItem]
    holdings: list[HoldingItem]


class TriggerSettingsView(BaseModel):
    model_config = ConfigDict(frozen=True)

    triggers: TriggerThresholds
    alerts: AlertConfig


class DataSettingsView(BaseModel):
    """Data source keys (masked) — EVDS macro + news APIs."""

    model_config = ConfigDict(frozen=True)

    evds_key: SecretStatus = Field(default_factory=lambda: SecretStatus(set=False))
    cryptopanic_token: SecretStatus = Field(default_factory=lambda: SecretStatus(set=False))
    finnhub_key: SecretStatus = Field(default_factory=lambda: SecretStatus(set=False))


class SettingsView(BaseModel):
    """Composite ``GET /api/settings`` response — secrets masked."""

    model_config = ConfigDict(frozen=True)

    llm: LLMSettingsView
    telegram: TelegramSettingsView
    watch: WatchlistSettingsView
    triggers: TriggerSettingsView
    data: DataSettingsView = Field(default_factory=DataSettingsView)


# ── POST /api/settings/* (write) ──────────────────────────────────────────────


class LLMSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    anthropic_api_key: str | None = None  # None=leave untouched; value=write to .env
    openai_api_key: str | None = None
    openrouter_api_key: str | None = None
    llm_provider: Literal["claude", "ollama"] | None = None
    ollama_base_url: str | None = None
    ollama_model: str | None = None
    # Per-role provider (claude|ollama|openai|openrouter). None=leave untouched.
    provider_routine: str | None = None
    provider_deep: str | None = None
    provider_copilot: str | None = None
    provider_news_router: str | None = None
    provider_exchange_expert: str | None = None  # BIST expert
    model_routine: str | None = None
    model_deep: str | None = None
    model_copilot: str | None = None
    model_news_router: str | None = None
    model_exchange_expert: str | None = None
    llm_daily_budget_usd: float | None = Field(default=None, ge=0.0)


class TelegramSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bot_token: str | None = None
    authorized_chat_ids: list[int] | None = None


class WatchlistUpdate(BaseModel):
    """Watchlist + holdings — **full list** replace (add/remove = send the list)."""

    model_config = ConfigDict(extra="forbid")

    watchlist: list[WatchlistItem]
    holdings: list[HoldingItem]


class TriggerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    triggers: TriggerThresholds
    alerts: AlertConfig


class DataSettingsUpdate(BaseModel):
    """Data source keys — write-only (None = leave untouched)."""

    model_config = ConfigDict(extra="forbid")

    evds_key: str | None = None
    cryptopanic_token: str | None = None
    finnhub_key: str | None = None


class SettingsWriteResult(BaseModel):
    """Write result — a ``.env`` change needs a restart to take effect live."""

    model_config = ConfigDict(frozen=True)

    ok: bool = True
    requires_restart: bool
    message: str = ""
