"""ConfigLoader.save (atomic YAML write) tests — offline, temporary directory."""

from __future__ import annotations

from pathlib import Path

import yaml

from argus.config.loader import ConfigLoader
from argus.config.models import HoldingItem, RuntimeConfig, WatchlistItem
from argus.core.enums import AssetClass


def _config() -> RuntimeConfig:
    return RuntimeConfig(
        watchlist=[
            WatchlistItem(symbol="BTCUSDT", asset_class=AssetClass.CRYPTO, tier="watchlist"),
            WatchlistItem(symbol="ACME", asset_class=AssetClass.BIST, note="Acme café №1"),
        ],
        holdings=[
            HoldingItem(
                symbol="BTCUSDT",
                asset_class=AssetClass.CRYPTO,
                quantity=0.5,
                avg_cost=58000,
                cost_currency="USD",
                owner="alice",
            )
        ],
    )


def _loader(tmp_path: Path) -> ConfigLoader:
    return ConfigLoader(config_dir=tmp_path, filename="config.yaml")


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    loader = _loader(tmp_path)
    loader.save(_config())
    loaded = loader.load()
    assert [w.symbol for w in loaded.watchlist] == ["BTCUSDT", "ACME"]
    assert loaded.holdings[0].quantity == 0.5
    assert loaded.holdings[0].owner == "alice"


def test_save_produces_valid_yaml(tmp_path: Path) -> None:
    loader = _loader(tmp_path)
    loader.save(_config())
    data = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert "watchlist" in data and "holdings" in data


def test_save_no_temp_file_left(tmp_path: Path) -> None:
    _loader(tmp_path).save(_config())
    leftovers = list(tmp_path.glob(".config.*.tmp"))
    assert leftovers == []


def test_save_preserves_unicode_note(tmp_path: Path) -> None:
    loader = _loader(tmp_path)
    loader.save(_config())
    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "Acme café №1" in text  # allow_unicode → written verbatim, not escaped


def test_save_refreshes_cache(tmp_path: Path) -> None:
    loader = _loader(tmp_path)
    loader.save(_config())
    # After save, get() returns the fresh config (without re-reading the file).
    assert loader.get().watchlist[0].symbol == "BTCUSDT"
    assert loader._mtime is not None  # mtime refreshed


def test_save_overwrites_atomically(tmp_path: Path) -> None:
    loader = _loader(tmp_path)
    loader.save(_config())
    # The second save (empty watchlist) fully replaces the previous one.
    loader.save(RuntimeConfig(watchlist=[], holdings=[]))
    assert loader.load().watchlist == []
