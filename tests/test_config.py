"""Settings + YAML config loader tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from argus.config import ConfigLoader, RuntimeConfig, Settings
from argus.config.models import WatchlistItem
from argus.core.enums import AssetClass
from argus.core.errors import ConfigError


def test_settings_defaults() -> None:
    s = Settings()
    assert s.env == "dev"
    assert s.is_prod is False
    assert s.llm_daily_budget_usd >= 0


def test_authorized_chat_id_parsing() -> None:
    s = Settings(authorized_chat_ids=" 111, 222 ,333 ")
    assert s.authorized_chat_id_set == frozenset({111, 222, 333})


def test_authorized_chat_id_empty() -> None:
    assert Settings(authorized_chat_ids="").authorized_chat_id_set == frozenset()


def test_secret_is_masked_in_repr() -> None:
    s = Settings(anthropic_api_key="fake-key-for-tests")
    assert "fake-key-for-tests" not in repr(s)
    assert s.anthropic_api_key is not None
    assert s.anthropic_api_key.get_secret_value() == "fake-key-for-tests"


def _write_config(tmp_path: Path, body: str) -> ConfigLoader:
    (tmp_path / "config.yaml").write_text(body, encoding="utf-8")
    return ConfigLoader(tmp_path)


def test_loader_reads_valid_config(tmp_path: Path) -> None:
    loader = _write_config(
        tmp_path,
        """
        version: 1
        watchlist:
          - symbol: BTCUSDT
            asset_class: crypto
            tier: watchlist
          - symbol: THYAO
            asset_class: bist
            tier: holdings
        """,
    )
    cfg = loader.load()
    assert isinstance(cfg, RuntimeConfig)
    assert len(cfg.watchlist) == 2
    assert cfg.by_tier("holdings") == [
        WatchlistItem(symbol="THYAO", asset_class=AssetClass.BIST, tier="holdings")
    ]


def test_loader_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        ConfigLoader(tmp_path).load()


def test_loader_invalid_config_raises(tmp_path: Path) -> None:
    loader = _write_config(tmp_path, "watchlist:\n  - symbol: X\n    asset_class: NOT_A_CLASS\n")
    with pytest.raises(ConfigError):
        loader.load()


def test_loader_unknown_field_forbidden(tmp_path: Path) -> None:
    loader = _write_config(tmp_path, "version: 1\nbogus: true\n")
    with pytest.raises(ConfigError):
        loader.load()


def test_runtime_config_default_alerts() -> None:
    cfg = RuntimeConfig()
    assert cfg.alerts.min_instant_severity == "high"
    assert cfg.alerts.quiet_start is None
    assert cfg.alerts.briefing_interval_s == 86_400


def test_loader_parses_alerts_block(tmp_path: Path) -> None:
    loader = _write_config(
        tmp_path,
        """
        version: 1
        watchlist: []
        alerts:
          min_instant_severity: medium
          quiet_start: "22:00"
          quiet_end: "07:00"
        """,
    )
    cfg = loader.load()
    assert cfg.alerts.min_instant_severity == "medium"
    assert cfg.alerts.quiet_start == "22:00"


def test_runtime_config_default_tunables() -> None:
    cfg = RuntimeConfig()
    assert cfg.loop.timeframe == "1h"
    assert cfg.loop.interval_for("holdings") == 300
    assert cfg.triggers.rsi_overbought == 70.0
    assert cfg.quality.max_jump_pct == 25.0
    assert cfg.universe.symbols == ["BTCUSDT", "ETHUSDT"]
    assert cfg.risk.live_orders_enabled is False
    assert cfg.risk.leverage == 0
    assert cfg.risk.human_approval_required is True


def test_runtime_config_rejects_unsafe_risk_combination() -> None:
    with pytest.raises(ValueError):
        RuntimeConfig.model_validate({"risk": {"live_orders_enabled": True}})
    with pytest.raises(ValueError):
        RuntimeConfig.model_validate({"risk": {"leverage": 2}})


def test_runtime_config_rejects_private_exchange_and_unsupported_book_depth() -> None:
    with pytest.raises(ValueError, match="public_data_only"):
        RuntimeConfig.model_validate(
            {"exchanges": [{"name": "binance", "enabled": True, "public_data_only": False}]}
        )
    with pytest.raises(ValueError, match="order_book_depth"):
        RuntimeConfig.model_validate({"ingestion": {"order_book_depth": 77}})
    with pytest.raises(ValueError, match="REST snapshot"):
        RuntimeConfig.model_validate(
            {
                "exchanges": [
                    {
                        "name": "binance",
                        "enabled": True,
                        "public_data_only": True,
                        "rest_enabled": False,
                        "websocket_enabled": True,
                    }
                ]
            }
        )


def test_runtime_config_rejects_symbols_outside_strict_universe() -> None:
    with pytest.raises(ValueError, match="strict universe"):
        RuntimeConfig.model_validate(
            {
                "universe": {"symbols": ["BTCUSDT", "ETHUSDT"], "strict": True},
                "watchlist": [{"symbol": "DOGEUSDT", "asset_class": "crypto", "tier": "watchlist"}],
            }
        )


def test_runtime_config_rejects_empty_or_duplicate_dimensions() -> None:
    with pytest.raises(ValueError, match="market_type"):
        RuntimeConfig.model_validate({"market_types": []})
    with pytest.raises(ValueError, match="timeframes must be unique"):
        RuntimeConfig.model_validate({"timeframes": ["1h", "1h"]})


def test_loader_parses_tunables(tmp_path: Path) -> None:
    loader = _write_config(
        tmp_path,
        """
        version: 1
        watchlist: []
        loop:
          timeframe: "15m"
          cooldown_s: 600
          holdings_interval_s: 60
        triggers:
          rsi_overbought: 75
        quality:
          max_jump_pct: 30
        """,
    )
    cfg = loader.load()
    assert cfg.loop.timeframe == "15m"
    assert cfg.loop.cooldown_s == 600
    assert cfg.loop.interval_for("holdings") == 60
    assert cfg.triggers.rsi_overbought == 75
    assert cfg.quality.max_jump_pct == 30


def test_reload_if_changed(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("version: 1\nwatchlist: []\n", encoding="utf-8")
    loader = ConfigLoader(tmp_path)
    loader.load()

    assert loader.reload_if_changed() is False  # unchanged

    path.write_text(
        "version: 2\nwatchlist:\n  - symbol: BTCUSDT\n    asset_class: crypto\n",
        encoding="utf-8",
    )
    # Force the mtime forward (keeps the test independent of filesystem resolution).
    future = os.path.getmtime(path) + 10
    os.utime(path, (future, future))

    assert loader.reload_if_changed() is True
    assert loader.get().version == 2
