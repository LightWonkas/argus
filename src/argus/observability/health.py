"""Health-check registry — aggregates component health.

Each component (redis, db, adapters, ...) registers a check function. The basic "self"
and "settings" checks are always defined.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from argus.core.schemas import utcnow

if TYPE_CHECKING:
    from argus.config.models import RuntimeConfig
    from argus.config.settings import Settings
    from argus.storage.sqlite import SqliteStore


class HealthState(StrEnum):
    """Health state of a check or of the system."""

    OK = "ok"
    DEGRADED = "degraded"
    FAIL = "fail"


@dataclass(frozen=True)
class CheckResult:
    """Result of a single health check."""

    name: str
    state: HealthState
    detail: str = ""


CheckFn = Callable[[], CheckResult]


@dataclass
class HealthRegistry:
    """Holds named health checks and produces an aggregate state."""

    _checks: dict[str, CheckFn] = field(default_factory=dict)

    def register(self, name: str, check: CheckFn) -> None:
        self._checks[name] = check

    def run(self) -> list[CheckResult]:
        """Run every check; a check that raises is reported as FAIL."""
        results: list[CheckResult] = []
        for name, check in self._checks.items():
            try:
                results.append(check())
            except Exception as exc:  # fail-safe: one check must not break the whole report
                results.append(CheckResult(name=name, state=HealthState.FAIL, detail=repr(exc)))
        return results

    def aggregate(self) -> HealthState:
        """Return the worst state (FAIL > DEGRADED > OK)."""
        results = self.run()
        states = {r.state for r in results}
        if HealthState.FAIL in states:
            return HealthState.FAIL
        if HealthState.DEGRADED in states:
            return HealthState.DEGRADED
        return HealthState.OK


def default_registry() -> HealthRegistry:
    """Return a registry with the basic checks registered."""
    registry = HealthRegistry()

    def _self_check() -> CheckResult:
        from argus import __version__

        return CheckResult(name="self", state=HealthState.OK, detail=f"argus {__version__}")

    def _settings_check() -> CheckResult:
        from argus.config.settings import get_settings

        settings = get_settings()
        return CheckResult(name="settings", state=HealthState.OK, detail=f"env={settings.env}")

    registry.register("self", _self_check)
    registry.register("settings", _settings_check)
    return registry


def operational_registry(
    settings: Settings,
    store: SqliteStore | None,
    runtime_config: RuntimeConfig | None = None,
) -> HealthRegistry:
    """Real, measurable operational checks for the dashboard.

    The dashboard process calls no external services. When the separate watch/WebSocket
    processes have sent no heartbeat it honestly reports ``DEGRADED`` instead of assuming
    health.
    """
    registry = default_registry()

    def _database() -> CheckResult:
        if store is None:
            return CheckResult("database", HealthState.FAIL, "SQLite is not configured")
        ok = store.ping()
        return CheckResult(
            "database",
            HealthState.OK if ok else HealthState.FAIL,
            "SQLite query succeeded" if ok else "SQLite query failed",
        )

    def _checkpoint(name: str, label: str, *, max_age_s: float) -> CheckResult:
        if store is None:
            return CheckResult(label, HealthState.DEGRADED, "telemetry store is not connected")
        raw = store.get_checkpoint(name)
        if raw is None:
            return CheckResult(label, HealthState.DEGRADED, "no heartbeat observed yet")
        try:
            age = (utcnow() - datetime.fromisoformat(raw)).total_seconds()
        except ValueError:
            return CheckResult(label, HealthState.FAIL, "invalid heartbeat timestamp")
        state = HealthState.OK if age <= max_age_s else HealthState.DEGRADED
        return CheckResult(label, state, f"last heartbeat {max(age, 0):.0f}s ago")

    def _last_market_data() -> CheckResult:
        if store is None:
            return CheckResult("last_market_data", HealthState.DEGRADED, "store is not connected")
        latest = store.latest_market_timestamp()
        if latest is None:
            return CheckResult("last_market_data", HealthState.DEGRADED, "DATA PENDING")
        age = (utcnow() - latest).total_seconds()
        state = HealthState.OK if age <= 86_400 else HealthState.DEGRADED
        return CheckResult("last_market_data", state, f"last market data {max(age, 0):.0f}s ago")

    def _websocket() -> CheckResult:
        if store is None:
            return CheckResult(
                "exchange_websocket", HealthState.DEGRADED, "telemetry store is not connected"
            )
        runtime_state = store.get_checkpoint("health.exchange_websocket.state")
        metrics_detail = ""
        metrics_raw = store.get_checkpoint("crypto:websocket:metrics")
        if metrics_raw is not None:
            try:
                metrics = json.loads(metrics_raw)
                metrics_detail = (
                    f"; last run records={int(metrics['total_records'])}, "
                    f"gaps={int(metrics['sequence_gaps'])}, "
                    f"write_failures={int(metrics['batch_write_failures'])}"
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                metrics_detail = "; last run metrics could not be read"
        if runtime_state == "stopped":
            return CheckResult(
                "exchange_websocket",
                HealthState.DEGRADED,
                f"stream process was stopped cleanly{metrics_detail}",
            )
        if runtime_state == "starting":
            return CheckResult(
                "exchange_websocket", HealthState.DEGRADED, "stream is starting; waiting for data"
            )
        if runtime_state not in (None, "running"):
            return CheckResult(
                "exchange_websocket",
                HealthState.FAIL,
                f"invalid stream runtime state: {runtime_state}",
            )
        result = _checkpoint("health.exchange_websocket", "exchange_websocket", max_age_s=120)
        return CheckResult(result.name, result.state, f"{result.detail}{metrics_detail}")

    def _llm() -> CheckResult:
        routine = settings.provider_for("routine")
        if routine == "claude" and settings.anthropic_api_key is None:
            return CheckResult("llm_provider", HealthState.DEGRADED, "Claude API key is not set")
        if routine not in ("claude", "ollama"):
            return CheckResult(
                "llm_provider", HealthState.DEGRADED, "provider is not active at runtime"
            )
        observed = store.get_checkpoint("health.llm") if store is not None else None
        detail = f"{routine} configured"
        if observed is None:
            detail += "; no successful call observed yet"
        return CheckResult(
            "llm_provider", HealthState.OK if observed else HealthState.DEGRADED, detail
        )

    def _telegram() -> CheckResult:
        configured = settings.telegram_bot_token is not None and bool(
            settings.authorized_chat_id_set
        )
        return CheckResult(
            "telegram",
            HealthState.DEGRADED,
            (
                "configured; this process has not tested the delivery connection"
                if configured
                else "token or authorized chat is missing"
            ),
        )

    def _guard() -> CheckResult:
        if runtime_config is None:
            return CheckResult("guard", HealthState.DEGRADED, "runtime config was not validated")
        risk = runtime_config.risk
        safe = (
            risk.spot_only
            and risk.leverage == 0
            and not risk.live_orders_enabled
            and risk.human_approval_required
            and risk.default_decision == "no_trade"
        )
        if not safe:
            return CheckResult("guard", HealthState.FAIL, "startup safety boundary is invalid")
        if risk.kill_switch:
            return CheckResult(
                "guard", HealthState.DEGRADED, "kill switch is active; decisions are halted"
            )
        return CheckResult(
            "guard",
            HealthState.OK,
            "spot-only, leverage=0, live orders disabled, human approval required",
        )

    def _shadow() -> CheckResult:
        if runtime_config is None:
            return CheckResult(
                "shadow_engine", HealthState.DEGRADED, "runtime config was not validated"
            )
        open_positions = len(store.shadow_positions(open_only=True)) if store is not None else 0
        if not runtime_config.shadow.enabled and open_positions:
            return CheckResult(
                "shadow_engine",
                HealthState.FAIL,
                f"disabled by config but {open_positions} open shadow positions exist",
            )
        if runtime_config.shadow.enabled:
            return CheckResult(
                "shadow_engine",
                HealthState.DEGRADED,
                f"enabled by config; open shadow positions={open_positions}; no live order path",
            )
        return CheckResult(
            "shadow_engine",
            HealthState.OK,
            "disabled by config; open shadow positions=0; no live order path",
        )

    def _storage_capacity() -> CheckResult:
        if store is None or runtime_config is None:
            return CheckResult(
                "storage_capacity",
                HealthState.DEGRADED,
                "storage retention measurement is not connected",
            )
        snapshot = store.storage_health(runtime_config.retention)
        if snapshot.healthy:
            return CheckResult(
                "storage_capacity",
                HealthState.OK,
                "disk/DB/backlog within policy limits",
            )
        state = (
            HealthState.FAIL
            if any("limit" in reason for reason in snapshot.reasons)
            else HealthState.DEGRADED
        )
        return CheckResult("storage_capacity", state, "; ".join(snapshot.reasons))

    def _retention() -> CheckResult:
        if store is None or runtime_config is None:
            return CheckResult(
                "retention", HealthState.DEGRADED, "retention measurement is not connected"
            )
        if not runtime_config.retention.enabled:
            return CheckResult("retention", HealthState.DEGRADED, "disabled by config")
        report = store.latest_retention_report()
        if report is None:
            return CheckResult("retention", HealthState.DEGRADED, "no retention run yet")
        if not report.success:
            return CheckResult("retention", HealthState.FAIL, report.reason or "retention failed")
        age = (utcnow() - report.finished_at).total_seconds()
        maximum_age = runtime_config.retention.maintenance_interval_s * 3
        return CheckResult(
            "retention",
            HealthState.OK if age <= maximum_age else HealthState.DEGRADED,
            f"last successful run {max(age, 0):.0f}s ago",
        )

    def _trading_control() -> CheckResult:
        if runtime_config is None:
            return CheckResult(
                "trading_control", HealthState.DEGRADED, "runtime config was not validated"
            )
        trading = runtime_config.trading
        fail_closed = (
            not trading.live_execution_enabled
            and trading.execution_backend == "disabled"
            and not trading.margin_enabled
            and not trading.futures_execution_enabled
            and not trading.withdrawals_enabled
            and not trading.transfers_enabled
        )
        if not fail_closed:
            return CheckResult(
                "trading_control", HealthState.FAIL, "execution safety boundary is open"
            )
        kill_switch = store.get_checkpoint("trading:kill_switch") if store is not None else None
        return CheckResult(
            "trading_control",
            HealthState.DEGRADED if kill_switch else HealthState.OK,
            (
                "kill switch record present; new missions/entries are blocked"
                if kill_switch
                else "DISARMED/public-only; no private execution adapter"
            ),
        )

    registry.register("database", _database)
    registry.register(
        "exchange_rest",
        lambda: _checkpoint("health.exchange_rest", "exchange_rest", max_age_s=3600),
    )
    registry.register("exchange_websocket", _websocket)
    registry.register("last_market_data", _last_market_data)
    registry.register(
        "event_bus", lambda: _checkpoint("health.event_bus", "event_bus", max_age_s=120)
    )
    registry.register("llm_provider", _llm)
    registry.register("telegram", _telegram)
    registry.register("guard", _guard)
    registry.register(
        "scheduler_heartbeat",
        lambda: _checkpoint("health.scheduler", "scheduler_heartbeat", max_age_s=3600),
    )
    registry.register("shadow_engine", _shadow)
    registry.register("storage_capacity", _storage_capacity)
    registry.register("retention", _retention)
    registry.register("trading_control", _trading_control)
    registry.register(
        "private_account",
        lambda: CheckResult(
            "private_account", HealthState.DEGRADED, "adapter is not connected; NO_DATA"
        ),
    )
    registry.register(
        "user_data_stream",
        lambda: CheckResult(
            "user_data_stream", HealthState.DEGRADED, "adapter is not connected; NO_DATA"
        ),
    )
    registry.register(
        "reconciliation",
        lambda: CheckResult(
            "reconciliation",
            HealthState.DEGRADED,
            "did not run because there is no private account",
        ),
    )
    return registry
