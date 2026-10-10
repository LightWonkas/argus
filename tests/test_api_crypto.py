"""Crypto cockpit API: stored PIT data only, auth, and an explicit missing-data status."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from argus.api.app import _crypto_dashboard_from_store, create_app
from argus.config.models import RuntimeConfig, UniverseConfig
from argus.core.crypto import SpotTicker
from argus.storage.sqlite import SqliteStore

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def _config() -> RuntimeConfig:
    return RuntimeConfig(universe=UniverseConfig(symbols=["BTCUSDT", "ETHUSDT"], strict=True))


def test_crypto_endpoint_returns_stored_price_and_never_invents_missing_fields() -> None:
    store = SqliteStore()
    try:
        store.save_crypto_record(
            SpotTicker(
                exchange="binance",
                symbol="BTCUSDT",
                event_timestamp=NOW,
                ingestion_timestamp=NOW,
                source="binance:public-rest",
                last_price=Decimal("60000.25"),
                bid=Decimal("60000"),
                ask=Decimal("60000.5"),
            )
        )
        app = create_app(
            crypto_dashboard_provider=lambda symbol: _crypto_dashboard_from_store(
                store, _config(), symbol
            )
        )
        with TestClient(app) as client:
            response = client.get("/api/crypto/BTCUSDT")
        assert response.status_code == 200
        body = response.json()
        assert body["symbol"] == "BTCUSDT"
        assert body["source"] == "stored_point_in_time"
        assert body["spot"]["last_price"] == "60000.25"
        assert body["perpetual"] is None
        assert body["funding"] is None
        assert body["data_status"] == "pending"
        assert "perpetual" in body["missing_data"]
    finally:
        store.close()


def test_crypto_endpoint_unknown_universe_is_explicitly_pending() -> None:
    store = SqliteStore()
    try:
        app = create_app(
            crypto_dashboard_provider=lambda symbol: _crypto_dashboard_from_store(
                store, _config(), symbol
            )
        )
        with TestClient(app) as client:
            body = client.get("/api/crypto/DOGEUSDT").json()
        assert body["data_status"] == "pending"
        assert body["spot"] is None
        assert body["missing_data"] == ["symbol_not_in_universe"]
    finally:
        store.close()
