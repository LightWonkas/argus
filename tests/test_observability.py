"""Health-check and logging tests."""

from __future__ import annotations

from argus.config.models import RuntimeConfig
from argus.config.settings import Settings
from argus.core.schemas import utcnow
from argus.observability import (
    CheckResult,
    HealthRegistry,
    HealthState,
    configure_logging,
    default_registry,
    get_logger,
)
from argus.observability.health import operational_registry
from argus.storage.sqlite import SqliteStore


def test_default_registry_is_ok() -> None:
    registry = default_registry()
    results = registry.run()
    names = {r.name for r in results}
    assert {"self", "settings"} <= names
    assert registry.aggregate() is HealthState.OK


def test_failing_check_is_captured_not_raised() -> None:
    registry = HealthRegistry()

    def _boom() -> CheckResult:
        raise RuntimeError("exploded")

    registry.register("boom", _boom)
    results = registry.run()
    assert results[0].state is HealthState.FAIL
    assert "exploded" in results[0].detail
    assert registry.aggregate() is HealthState.FAIL


def test_aggregate_degraded() -> None:
    registry = HealthRegistry()
    registry.register("ok", lambda: CheckResult("ok", HealthState.OK))
    registry.register("deg", lambda: CheckResult("deg", HealthState.DEGRADED))
    assert registry.aggregate() is HealthState.DEGRADED


def test_logging_configures_without_error() -> None:
    configure_logging(level="DEBUG", fmt="json")
    log = get_logger("test")
    log.info("sample-event", k="v")  # must not raise


def test_operational_registry_lists_required_components_without_network() -> None:
    store = SqliteStore()
    registry = operational_registry(Settings(_env_file=None), store, RuntimeConfig())
    names = {result.name for result in registry.run()}
    assert {
        "database",
        "exchange_rest",
        "exchange_websocket",
        "last_market_data",
        "event_bus",
        "llm_provider",
        "telegram",
        "guard",
        "scheduler_heartbeat",
        "shadow_engine",
        "storage_capacity",
        "retention",
        "trading_control",
        "private_account",
        "user_data_stream",
        "reconciliation",
    } <= names
    assert next(r for r in registry.run() if r.name == "database").state is HealthState.OK
    assert next(r for r in registry.run() if r.name == "guard").state is HealthState.OK
    shadow = next(r for r in registry.run() if r.name == "shadow_engine")
    assert shadow.state is HealthState.OK
    assert "disabled by config" in shadow.detail
    store.close()


def test_operational_registry_does_not_claim_unverified_runtime_state() -> None:
    store = SqliteStore()
    registry = operational_registry(Settings(_env_file=None), store)
    results = registry.run()
    assert next(r for r in results if r.name == "guard").state is HealthState.DEGRADED
    assert next(r for r in results if r.name == "shadow_engine").state is HealthState.DEGRADED
    store.close()


def test_operational_registry_reports_gracefully_stopped_websocket_as_degraded() -> None:
    store = SqliteStore()
    store.set_checkpoint("health.exchange_websocket", utcnow().isoformat())
    store.set_checkpoint("health.exchange_websocket.state", "stopped")
    try:
        result = next(
            r
            for r in operational_registry(Settings(_env_file=None), store, RuntimeConfig()).run()
            if r.name == "exchange_websocket"
        )
        assert result.state is HealthState.DEGRADED
        assert "stopped cleanly" in result.detail
    finally:
        store.close()


def test_operational_registry_exposes_persisted_websocket_run_metrics() -> None:
    store = SqliteStore()
    store.set_checkpoint("health.exchange_websocket.state", "stopped")
    store.set_checkpoint(
        "crypto:websocket:metrics",
        '{"total_records":42,"sequence_gaps":1,"batch_write_failures":0}',
    )
    try:
        result = next(
            r
            for r in operational_registry(Settings(_env_file=None), store, RuntimeConfig()).run()
            if r.name == "exchange_websocket"
        )
        assert "records=42" in result.detail
        assert "gaps=1" in result.detail
        assert "write_failures=0" in result.detail
    finally:
        store.close()
