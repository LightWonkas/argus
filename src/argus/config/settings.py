"""Infrastructure + secret configuration — read from environment variables (``.env``).

Secrets (API keys, tokens) are held here as :class:`~pydantic.SecretStr` and are
**never written to logs**. The editable "data" config (watchlist, triggers,
thresholds, risk limits) lives elsewhere → see :mod:`argus.config.loader`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from the environment (``ARGUS_*`` prefix)."""

    model_config = SettingsConfigDict(
        env_prefix="ARGUS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # ── General ───────────────────────────────────────────────────────────────
    env: Literal["dev", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["console", "json"] = "console"
    timezone: str = "Europe/Istanbul"

    # ── Event bus / storage ────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"
    database_url: str = "sqlite:///data/argus.db"

    # ── Data sources ───────────────────────────────────────────────────────────
    # CBRT EVDS key (https://evds2.tcmb.gov.tr — free registration). Without it the macro feed
    # is honestly SKIPPED (no fake macro data; MacroIntermarketAgent reports NO_DATA).
    evds_key: SecretStr | None = Field(default=None, validation_alias="ARGUS_EVDS_KEY")
    # News APIs (free tiers): without a token that source is honestly SKIPPED.
    cryptopanic_token: SecretStr | None = Field(
        default=None, validation_alias="ARGUS_CRYPTOPANIC_TOKEN"
    )
    finnhub_key: SecretStr | None = Field(default=None, validation_alias="ARGUS_FINNHUB_KEY")

    # ── LLM / AI (managed from the Models page; some read from unprefixed env names) ──
    # Keys are per provider (not per role): one Anthropic key covers every Claude role.
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="ARGUS_OPENAI_API_KEY")
    openrouter_api_key: SecretStr | None = Field(
        default=None, validation_alias="ARGUS_OPENROUTER_API_KEY"
    )
    # Global provider — backwards compatibility only (fallback when `provider_*` is unset).
    llm_provider: Literal["claude", "ollama"] = "claude"
    ollama_base_url: str = "http://localhost:11434"
    llm_daily_budget_usd: float = Field(default=3.0, ge=0.0)
    # Role → provider + model. Each role picks its own provider (claude|ollama|openai|openrouter).
    # Empty → falls back to `llm_provider` (old .env compatibility). routine: scans/alerts ·
    # deep: /analyze · copilot: cockpit chat. Model name is provider-specific (claude-…, gpt-4o, …).
    provider_routine: str = ""
    provider_deep: str = ""
    provider_copilot: str = ""
    provider_news_router: str = ""  # multi-AI: news router agent
    # BIST expert — defaults to OLLAMA ($0). Do not change the model without a golden eval.
    provider_exchange_expert: str = "ollama"
    model_routine: str = "claude-sonnet-4-6"
    model_deep: str = "claude-opus-4-8"
    model_copilot: str = "claude-sonnet-4-6"
    model_news_router: str = "claude-sonnet-4-6"  # news classification — cheap/fast is enough
    model_exchange_expert: str = ""  # empty → falls back to ollama_model (build_router compat)
    # Add the BIST expert to the confluence set (never added in demo mode). To turn it off set
    # ARGUS_ENABLE_EXCHANGE_EXPERT=false (e.g. during a 4-6 week baseline-comparison period).
    enable_exchange_expert: bool = True
    # Default Ollama model — fallback for an empty role model / old ".env" (now free per role).
    ollama_model: str = "llama3.2"

    # ── Telegram ───────────────────────────────────────────────────────────────
    telegram_bot_token: SecretStr | None = Field(
        default=None, validation_alias="TELEGRAM_BOT_TOKEN"
    )
    # Authorized chat ids are kept raw (comma-separated); see the property below for parsing.
    authorized_chat_ids: str = ""

    # ── Dashboard / web cockpit ────────────────────────────────────
    # Users are "name:scrypt$hash,name2:scrypt$hash2" (passwords NEVER in plain text or code).
    # Create hashes with argus.api.security.hash_password. Empty → auth disabled (dev only).
    dashboard_users: SecretStr = Field(
        default=SecretStr(""), validation_alias="ARGUS_DASHBOARD_USERS"
    )
    # Secret key for signing the session cookie (empty → random per process).
    dashboard_session_secret: SecretStr = Field(
        default=SecretStr(""), validation_alias="ARGUS_DASHBOARD_SESSION_SECRET"
    )
    # Interface to listen on — defaults to localhost (do not bind publicly).
    dashboard_bind: str = "127.0.0.1"
    dashboard_port: int = Field(default=8787, ge=1, le=65535)
    # Allowed CORS origins (comma-separated). In dev the frontend is e.g.
    # "http://localhost:3000". Empty → CORS off (same-origin). Wildcards are not used.
    dashboard_cors_origins: str = ""
    # Optional TradingView webhook authentication. Never placed in payloads or API responses.
    tradingview_webhook_secret: SecretStr | None = Field(
        default=None, validation_alias="ARGUS_TRADINGVIEW_WEBHOOK_SECRET"
    )

    @property
    def authorized_chat_id_set(self) -> frozenset[int]:
        """Authorized Telegram chat ids (authorized users only)."""
        ids: set[int] = set()
        for part in self.authorized_chat_ids.split(","):
            part = part.strip()
            if part:
                ids.add(int(part))
        return frozenset(ids)

    def provider_for(self, role: str) -> str:
        """Provider for a role — falls back to `llm_provider` when `provider_<role>` is empty."""
        per_role = getattr(self, f"provider_{role}", "") or ""
        return per_role.strip() or self.llm_provider

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Returns the single (cached) ``Settings`` instance for the process."""
    return Settings()
