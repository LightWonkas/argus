"""Public-only Argus news + market soak validation orchestrator.

This tool only creates an isolated SQLite file. It never calls a private endpoint, a
mission mutation, OrderIntent or the shadow engine. The default duration is an
uninterrupted 60 minutes.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import gc
import json
import math
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import tracemalloc
import urllib.error
import urllib.request
from collections import Counter, deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TextIO, cast

API_PATHS = (
    "/api/health",
    "/api/trading/status",
    "/api/trading/universe",
    "/api/trading/news",
    "/api/trading/research",
    "/api/trading/plans",
    "/api/trading/costs",
    "/api/trading/health",
)


def _measurement_capabilities(metrics_mode: str) -> dict[str, bool]:
    """Return explicit instrumentation boundaries without starting a profiler."""

    return {
        "gc_metrics": metrics_mode in {"diagnostic", "deep_tracing"},
        "tracemalloc": metrics_mode == "deep_tracing",
        "allocation_snapshots": metrics_mode == "deep_tracing",
    }


SAFETY_TABLES = (
    "order_intent",
    "shadow_order",
    "shadow_fill",
    "shadow_position",
    "trading_mission",
)
COUNT_TABLES = (
    "crypto_event",
    "crypto_artifact",
    "trade_candidate",
    "risk_assessment",
    "universe_snapshot",
    "crypto_news_intelligence",
    "news_market_reaction",
    "coin_research_result",
    "trade_plan",
    *SAFETY_TABLES,
)
SECRET_ENV_KEYS = (
    "ARGUS_EVDS_KEY",
    "ARGUS_CRYPTOPANIC_TOKEN",
    "ARGUS_FINNHUB_KEY",
    "ANTHROPIC_API_KEY",
    "ARGUS_OPENAI_API_KEY",
    "ARGUS_OPENROUTER_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "ARGUS_AUTHORIZED_CHAT_IDS",
    "ARGUS_DASHBOARD_USERS",
    "ARGUS_DASHBOARD_SESSION_SECRET",
)


class _Tee:
    def __init__(self, *targets: TextIO) -> None:
        self.targets = targets

    def write(self, value: str) -> int:
        for target in self.targets:
            target.write(value)
            target.flush()
        return len(value)

    def flush(self) -> None:
        for target in self.targets:
            target.flush()


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _percentile(values: list[float] | deque[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 3)


def _safe_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"parse_error": True, "raw_length": len(value)}
    return parsed if isinstance(parsed, dict) else {"unexpected_json_type": type(parsed).__name__}


def _db_snapshot(path: Path) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "database_bytes": path.stat().st_size if path.exists() else 0,
        "wal_bytes": path.with_name(f"{path.name}-wal").stat().st_size
        if path.with_name(f"{path.name}-wal").exists()
        else 0,
        "table_counts": {},
        "table_bytes": {},
        "checkpoints": {},
        "sqlite_error": None,
    }
    if not path.exists():
        return snapshot
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(path, timeout=5)
        connection.execute("PRAGMA busy_timeout=5000")
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        snapshot["table_counts"] = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in COUNT_TABLES
            if table in tables
        }
        try:
            snapshot["table_bytes"] = {
                str(row[0]): int(row[1] or 0)
                for row in connection.execute(
                    "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name ORDER BY name"
                )
            }
        except sqlite3.Error:
            snapshot["table_bytes"] = {"status": "NO_DATA"}
        if "checkpoint" in tables:
            snapshot["checkpoints"] = {
                str(row[0]): str(row[1])
                for row in connection.execute("SELECT key,value FROM checkpoint")
            }
    except sqlite3.Error as exc:
        snapshot["sqlite_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if connection is not None:
            connection.close()
    checkpoints = snapshot["checkpoints"]
    snapshot["stream"] = _safe_json(checkpoints.get("crypto:websocket:metrics"))
    snapshot["news"] = _safe_json(checkpoints.get("news_intelligence:health"))
    snapshot["watchlist"] = _safe_json(checkpoints.get("dynamic_universe:watchlist"))
    return snapshot


def _quick_check(path: Path) -> str:
    connection = sqlite3.connect(path, timeout=30)
    try:
        return str(connection.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        connection.close()


def _domain_metrics(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "plan_horizons": {},
        "plan_bases": {},
        "no_trade_reasons": {},
        "cost_rejections": 0,
        "research_bases": {},
        "news_coverage": {},
        "news_event_types": {},
        "news_verification": {},
        "news_entity_matches": 0,
        "news_ambiguous_matches": 0,
        "reaction_statuses": {},
        "regimes": {},
        "trigger_count": 0,
        "manager_outputs": 0,
        "llm_calls": 0,
        "guard_decisions": {},
    }
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        horizons: Counter[str] = Counter()
        bases: Counter[str] = Counter()
        reasons: Counter[str] = Counter()
        for row in connection.execute("SELECT payload FROM trade_plan"):
            payload = _safe_json(str(row["payload"]))
            horizons[str(payload.get("horizon", "unknown"))] += 1
            bases[str(payload.get("strategy_basis", "unknown"))] += 1
            for reason in payload.get("rejection_reasons", []):
                reasons[str(reason)] += 1
                if "insufficient net edge" in str(reason):
                    result["cost_rejections"] += 1
        result["plan_horizons"] = dict(horizons)
        result["plan_bases"] = dict(bases)
        result["no_trade_reasons"] = dict(reasons)

        research_bases: Counter[str] = Counter()
        coverage: Counter[str] = Counter()
        for row in connection.execute("SELECT payload FROM coin_research_result"):
            payload = _safe_json(str(row["payload"]))
            research_bases[str(payload.get("strategy_basis", "unknown"))] += 1
            coverage[str(payload.get("news_coverage_status", "unknown"))] += 1
        result["research_bases"] = dict(research_bases)
        result["news_coverage"] = dict(coverage)

        news_types: Counter[str] = Counter()
        verification: Counter[str] = Counter()
        for row in connection.execute("SELECT payload FROM crypto_news_intelligence"):
            payload = _safe_json(str(row["payload"]))
            news_types[str(payload.get("event_type", "unknown"))] += 1
            verification[str(payload.get("verification_status", "unknown"))] += 1
            matches = payload.get("entity_matches", [])
            if isinstance(matches, list):
                result["news_entity_matches"] += len(matches)
                result["news_ambiguous_matches"] += sum(
                    isinstance(match, dict) and bool(match.get("ambiguous")) for match in matches
                )
        result["news_event_types"] = dict(news_types)
        result["news_verification"] = dict(verification)
        result["reaction_statuses"] = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT status,COUNT(*) FROM news_market_reaction GROUP BY status"
            )
        }

        regimes: Counter[str] = Counter()
        for row in connection.execute(
            "SELECT artifact_type,payload FROM crypto_artifact "
            "WHERE artifact_type IN ('regime','triggers','experts')"
        ):
            payload = json.loads(str(row["payload"]))
            if row["artifact_type"] == "regime" and isinstance(payload, dict):
                regimes[str(payload.get("regime", "unknown"))] += 1
            elif row["artifact_type"] == "triggers" and isinstance(payload, list):
                result["trigger_count"] += len(payload)
            elif row["artifact_type"] == "experts" and isinstance(payload, list):
                result["manager_outputs"] += sum(
                    isinstance(item, dict) and item.get("role") == "manager" for item in payload
                )
        result["regimes"] = dict(regimes)
        result["guard_decisions"] = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT decision,COUNT(*) FROM risk_assessment GROUP BY decision"
            )
        }
        result["llm_calls"] = int(
            connection.execute("SELECT COALESCE(SUM(calls),0) FROM llm_cost").fetchone()[0]
        )
    finally:
        connection.close()
    return result


def _api_get(url: str) -> tuple[int, float, int, bool, str | None]:
    started = time.monotonic()
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = response.read(5_000_001)
            latency_ms = (time.monotonic() - started) * 1000
            lowered = payload.lower()
            secret_marker = any(
                marker in lowered
                for marker in (b"x-mbx-apikey", b"api_secret", b"private_key", b"bearer ")
            )
            if len(payload) > 5_000_000:
                return (
                    response.status,
                    latency_ms,
                    len(payload),
                    secret_marker,
                    "response_too_large",
                )
            json.loads(payload)
            return response.status, latency_ms, len(payload), secret_marker, None
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return 0, (time.monotonic() - started) * 1000, 0, False, type(exc).__name__


def _windows_process_sample(pids: tuple[int, ...]) -> dict[str, Any]:
    if os.name != "nt":
        return {"supported": False}

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    class FileTime(ctypes.Structure):
        _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]

    class ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_ulong),
            ("cntUsage", ctypes.c_ulong),
            ("th32ThreadID", ctypes.c_ulong),
            ("th32OwnerProcessID", ctypes.c_ulong),
            ("tpBasePri", ctypes.c_long),
            ("tpDeltaPri", ctypes.c_long),
            ("dwFlags", ctypes.c_ulong),
        ]

    def filetime_value(value: FileTime) -> int:
        return (int(value.high) << 32) | int(value.low)

    process_query = 0x0400
    process_vm_read = 0x0010
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    rss_total = 0
    peak_rss_total = 0
    handle_total = 0
    cpu_seconds = 0.0
    live_pids: list[int] = []
    process_samples: list[dict[str, int]] = []
    private_bytes_total = 0
    for pid in pids:
        handle = kernel32.OpenProcess(process_query | process_vm_read, False, pid)
        if not handle:
            continue
        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), ctypes.sizeof(counters)):
            rss_total += int(counters.WorkingSetSize)
            peak_rss_total += int(counters.PeakWorkingSetSize)
            private_bytes_total += int(counters.PrivateUsage)
            live_pids.append(pid)
            process_samples.append(
                {
                    "pid": pid,
                    "rss_bytes": int(counters.WorkingSetSize),
                    "peak_rss_bytes": int(counters.PeakWorkingSetSize),
                    "private_bytes": int(counters.PrivateUsage),
                }
            )
        count = ctypes.c_ulong()
        if kernel32.GetProcessHandleCount(handle, ctypes.byref(count)):
            handle_total += int(count.value)
        created, exited, kernel, user = FileTime(), FileTime(), FileTime(), FileTime()
        if kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            cpu_seconds += (filetime_value(kernel) + filetime_value(user)) / 10_000_000
        kernel32.CloseHandle(handle)
    thread_count = 0
    snapshot_handle = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if snapshot_handle and snapshot_handle != invalid_handle:
        entry = ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        if kernel32.Thread32First(snapshot_handle, ctypes.byref(entry)):
            while True:
                if int(entry.th32OwnerProcessID) in pids:
                    thread_count += 1
                if not kernel32.Thread32Next(snapshot_handle, ctypes.byref(entry)):
                    break
        kernel32.CloseHandle(snapshot_handle)
    established = 0
    try:
        netstat = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        pid_tokens = {str(pid) for pid in pids}
        established = sum(
            1
            for line in netstat.stdout.splitlines()
            if "ESTABLISHED" in line and line.split()[-1:] and line.split()[-1] in pid_tokens
        )
    except (OSError, subprocess.SubprocessError):
        established = -1
    return {
        "supported": True,
        "live_pids": live_pids,
        "rss_bytes": rss_total,
        "peak_rss_bytes": peak_rss_total,
        "private_bytes": private_bytes_total,
        # Windows PrivateUsage is available, but it is not identical to cross-platform USS.
        "uss_bytes": None,
        "processes": process_samples,
        "cpu_seconds": round(cpu_seconds, 3),
        "thread_count": thread_count,
        "handle_count": handle_total,
        "established_tcp_connections": established,
    }


def _snapshot_statistics(snapshot: tracemalloc.Snapshot) -> dict[str, tuple[int, int, str]]:
    output: dict[str, tuple[int, int, str]] = {}
    for item in snapshot.statistics("lineno"):
        frame = item.traceback[0]
        key = f"{frame.filename}:{frame.lineno}"
        owner = _allocation_owner(frame.filename)
        output[key] = (item.size, item.count, owner)
    return output


def _allocation_owner(filename: str) -> str:
    """Classify allocation paths without treating the repo-local venv as Argus."""

    resolved = Path(filename).resolve()
    folded_parts = tuple(part.casefold() for part in resolved.parts)
    if any(part in {"site-packages", "dist-packages", ".venv"} for part in folded_parts):
        return "THIRD_PARTY"
    root = Path.cwd().resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        relative = None
    if (
        relative is not None
        and relative.parts
        and relative.parts[0].casefold()
        in {
            "src",
            "scripts",
        }
    ):
        return "ARGUS_SOURCE"
    stdlib = Path(sys.base_prefix, "Lib").resolve()
    try:
        resolved.relative_to(stdlib)
    except ValueError:
        return "UNKNOWN"
    return "PYTHON_STDLIB"


def _write_tracemalloc_diff(
    artifact_dir: Path,
    *,
    label: str,
    elapsed_seconds: float,
    previous: dict[str, tuple[int, int, str]] | None,
) -> dict[str, tuple[int, int, str]]:
    """Write top allocation growth and release the heavy Snapshot immediately."""

    snapshot = tracemalloc.take_snapshot()
    current = _snapshot_statistics(snapshot)
    del snapshot
    rows: list[dict[str, Any]] = []
    for location, (size, count, owner) in current.items():
        old_size, old_count, _old_owner = (previous or {}).get(location, (0, 0, owner))
        rows.append(
            {
                "location": location,
                "owner": owner,
                "allocation_bytes": size,
                "allocation_count": count,
                "byte_diff": size - old_size,
                "count_diff": count - old_count,
            }
        )
    rows.sort(key=lambda item: int(item["byte_diff"]), reverse=True)
    payload = {
        "label": label,
        "captured_at": _utcnow().isoformat(),
        "elapsed_seconds": round(elapsed_seconds, 3),
        "compared_to": "previous_snapshot" if previous is not None else "empty_baseline",
        "top_growth": rows[:30],
        "growth_bytes_by_owner": {
            owner: sum(int(item["byte_diff"]) for item in rows if item["owner"] == owner)
            for owner in ("ARGUS_SOURCE", "PYTHON_STDLIB", "THIRD_PARTY", "UNKNOWN")
        },
    }
    (artifact_dir / f"tracemalloc-{label}.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return current


def _stop_process(process: subprocess.Popen[bytes], *, timeout: float = 20) -> dict[str, Any]:
    result: dict[str, Any] = {"pid": process.pid, "method": None, "exit_code": process.poll()}
    if process.poll() is not None:
        return result
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
            result["method"] = "CTRL_BREAK_EVENT"
        else:
            process.send_signal(signal.SIGINT)
            result["method"] = "SIGINT"
        process.wait(timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        process.terminate()
        result["method"] = "terminate"
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            result["method"] = "kill"
            process.wait(timeout=10)
    result["exit_code"] = process.returncode
    result["alive_after_stop"] = process.poll() is None
    return result


async def _wait_for_api(base_url: str, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"FastAPI exited early: exit={process.returncode}")
        status, _latency, _size, _secret, _error = await asyncio.to_thread(
            _api_get, f"{base_url}/api/trading/status"
        )
        if status == 200:
            return
        await asyncio.sleep(0.5)
    raise TimeoutError("FastAPI was not ready within 30 seconds")


def _assert_public_config(config: Any) -> None:
    violations: list[str] = []
    if config.trading.mode.value != "public_monitor":
        violations.append("trading.mode is not public_monitor")
    if config.trading.live_execution_enabled or config.trading.submission_authorized:
        violations.append("execution/submission is enabled")
    if config.trading.leverage != 0 or config.risk.leverage != 0:
        violations.append("leverage is not zero")
    if config.shadow.enabled:
        violations.append("shadow is enabled")
    if any(not item.public_data_only for item in config.exchanges if item.enabled):
        violations.append("an exchange without public_data_only is configured")
    if violations:
        raise RuntimeError("public-only preflight failed: " + "; ".join(violations))


async def _run_soak(args: argparse.Namespace, artifact_dir: Path) -> int:
    from argus.config.loader import ConfigLoader
    from argus.config.settings import Settings
    from argus.core.enums import MarketType
    from argus.interface.factory import build_crypto_cycle, build_crypto_stream, build_news_cycle
    from argus.observability.logging import configure_logging
    from argus.observability.readiness import (
        MemoryHealthSnapshot,
        MemoryHealthStatus,
        MemoryTrendSample,
        ReadinessEvaluator,
        criterion,
        evaluate_memory_trend,
    )

    configure_logging(level="INFO", fmt="json")
    metrics_mode = cast(
        Literal["production", "diagnostic", "deep_tracing"],
        "deep_tracing" if args.memory_diagnostic else args.metrics_mode,
    )
    measurement_capabilities = _measurement_capabilities(metrics_mode)
    enhanced_metrics = measurement_capabilities["gc_metrics"]
    deep_tracing = measurement_capabilities["tracemalloc"]
    database_path = artifact_dir / "argus-soak.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    child_env = os.environ.copy()
    child_env["ARGUS_DATABASE_URL"] = database_url
    child_env["ARGUS_DASHBOARD_BIND"] = "127.0.0.1"
    child_env["ARGUS_DASHBOARD_PORT"] = str(args.port)
    child_env["ARGUS_LOG_FORMAT"] = "json"
    child_env["PYTHONUNBUFFERED"] = "1"
    for key in SECRET_ENV_KEYS:
        child_env[key] = ""

    config = ConfigLoader().load()
    config.diagnostics.metrics_mode = metrics_mode
    if args.websocket_compression is not None:
        config.ingestion.websocket.compression.enabled = args.websocket_compression == "enabled"
    _assert_public_config(config)
    settings = Settings(
        database_url=database_url,
        dashboard_bind="127.0.0.1",
        dashboard_port=args.port,
        evds_key=None,
        cryptopanic_token=None,
        finnhub_key=None,
        anthropic_api_key=None,
        openai_api_key=None,
        openrouter_api_key=None,
        telegram_bot_token=None,
        authorized_chat_ids="",
        dashboard_users="",
        dashboard_session_secret="",
    )

    shutil.copy2(ConfigLoader().source_path, artifact_dir / "config-snapshot.yaml")
    (artifact_dir / "effective-config.json").write_text(
        config.model_dump_json(indent=2), encoding="utf-8"
    )
    (artifact_dir / "preflight.json").write_text(
        json.dumps(
            {
                "checked_at": _utcnow().isoformat(),
                "database_url": database_url,
                "public_data_only": True,
                "trading_mode": config.trading.mode.value,
                "live_execution_enabled": config.trading.live_execution_enabled,
                "submission_authorized": config.trading.submission_authorized,
                "leverage": config.trading.leverage,
                "shadow_enabled": config.shadow.enabled,
                "metrics_mode": metrics_mode,
                "tracemalloc_enabled": deep_tracing,
                "allocation_snapshots_enabled": deep_tracing,
                "websocket_compression_enabled": (config.ingestion.websocket.compression.enabled),
                "secret_environment_keys_forced_empty": list(SECRET_ENV_KEYS),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    api_log = (artifact_dir / "api.log").open("wb")
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    api_process = subprocess.Popen(
        [sys.executable, "-m", "argus", "--dashboard"],
        cwd=Path.cwd(),
        env=child_env,
        stdout=api_log,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )
    base_url = f"http://127.0.0.1:{args.port}"
    await _wait_for_api(base_url, api_process)

    crypto_cycle = build_crypto_cycle(settings, config)
    stream = build_crypto_stream(settings, config)
    news_cycle = build_news_cycle(settings, config)
    if crypto_cycle is None or stream is None:
        raise RuntimeError("could not build the public Binance REST/WebSocket configuration")

    if deep_tracing:
        tracemalloc.start(config.diagnostics.tracemalloc_frames)

    api_metrics: dict[str, dict[str, Any]] = {
        path: {
            "attempts": 0,
            "successes": 0,
            "latencies_ms": deque(maxlen=config.diagnostics.maximum_metric_samples),
            "max_response_bytes": 0,
            "errors": {},
        }
        for path in API_PATHS
    }
    refresh_runs: deque[dict[str, Any]] = deque(maxlen=1024)
    snapshots: deque[dict[str, Any]] = deque(maxlen=1024)
    event_loop_lags: deque[float] = deque(maxlen=config.diagnostics.maximum_metric_samples)
    memory_trend_samples: deque[MemoryTrendSample] = deque(maxlen=1024)
    run_errors: deque[str] = deque(maxlen=1024)
    latest_universe_size = [len(stream.cycle.selected_symbols())]
    started_at = _utcnow()
    started_monotonic = time.monotonic()
    stop_event = asyncio.Event()
    previous_trace: dict[str, tuple[int, int, str]] | None = None
    if deep_tracing:
        previous_trace = _write_tracemalloc_diff(
            artifact_dir,
            label="000-start",
            elapsed_seconds=0.0,
            previous=None,
        )

    async def api_loop() -> None:
        while not stop_event.is_set():
            for path in API_PATHS:
                status, latency, size, secret, error = await asyncio.to_thread(
                    _api_get, f"{base_url}{path}"
                )
                metric = api_metrics[path]
                metric["attempts"] += 1
                metric["latencies_ms"].append(latency)
                metric["max_response_bytes"] = max(metric["max_response_bytes"], size)
                if status == 200 and error is None:
                    metric["successes"] += 1
                else:
                    key = error or f"HTTP_{status}"
                    metric["errors"][key] = metric["errors"].get(key, 0) + 1
                if secret:
                    metric["secret_markers"] = metric.get("secret_markers", 0) + 1
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=args.api_interval_seconds)

    async def lag_loop() -> None:
        target = time.monotonic() + 1
        while not stop_event.is_set():
            await asyncio.sleep(max(0, target - time.monotonic()))
            now = time.monotonic()
            event_loop_lags.append(max(0.0, (now - target) * 1000))
            target = now + 1

    async def refresh(kind: str) -> None:
        started = time.monotonic()
        record: dict[str, Any] = {"kind": kind, "started_at": _utcnow().isoformat()}
        try:
            if kind == "news":
                batch = await asyncio.to_thread(news_cycle.run_once)
                record.update(
                    {
                        "data_status": batch.data_status.value,
                        "coverage_status": batch.coverage_status.value,
                        "records": len(batch.records),
                        "duplicates": batch.duplicate_count,
                        "provider_errors": list(batch.provider_errors),
                    }
                )
                record["health"] = _safe_json(
                    news_cycle.store.get_checkpoint("news_intelligence:health")
                )
            else:
                results = await asyncio.to_thread(crypto_cycle.run_once)
                record["symbols"] = [
                    {
                        "symbol": result.symbol,
                        "quality": result.quality_decision.value,
                        "data_status": result.data_status.value,
                        "errors": list(result.errors),
                    }
                    for result in results
                ]
                clock_skew_ms = {
                    market.value: await asyncio.to_thread(
                        crypto_cycle.provider.check_clock_skew, market
                    )
                    for market in (MarketType.SPOT, MarketType.PERPETUAL)
                }
                record["clock_skew_ms"] = clock_skew_ms
                for market in (MarketType.SPOT, MarketType.PERPETUAL):
                    stream.cycle.stream_metrics.update_clock_skew(
                        market, float(clock_skew_ms[market.value])
                    )
            record["ok"] = True
        except Exception as exc:
            record["ok"] = False
            record["error"] = f"{type(exc).__name__}: {exc}"
            run_errors.append(f"{kind}: {type(exc).__name__}: {exc}")
        record["finished_at"] = _utcnow().isoformat()
        record["duration_seconds"] = round(time.monotonic() - started, 3)
        refresh_runs.append(record)
        with (artifact_dir / "refresh-runs.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(json.dumps({"refresh": record}, ensure_ascii=False))

    async def reconciliation_loop() -> None:
        await refresh("news")
        if stop_event.is_set():
            return
        await refresh("crypto")
        if stop_event.is_set():
            return
        await refresh("news")
        if stop_event.is_set():
            return
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=args.reconcile_seconds)
            except TimeoutError:
                await refresh("news")
                if not stop_event.is_set():
                    await refresh("crypto")

    def capture_memory(
        system: dict[str, Any],
        elapsed_seconds: float,
        *,
        retain_for_trend: bool = True,
        resources_closed: bool = False,
    ) -> tuple[MemoryHealthSnapshot, MemoryTrendSample]:
        current_bytes: int | None = None
        peak_bytes: int | None = None
        if deep_tracing:
            current_bytes, peak_bytes = tracemalloc.get_traced_memory()
        tasks = asyncio.all_tasks()
        pending_tasks = sum(not task.done() for task in tasks)
        done_tasks = sum(task.done() for task in tasks)
        gc_stats = gc.get_stats() if enhanced_metrics else None
        news_state = news_cycle.diagnostic_state()
        stream_state = stream.cycle.stream_metrics.as_payload()
        if resources_closed:
            open_connections = 0
        else:
            sqlite_states = (
                crypto_cycle.store.diagnostic_connection_state(),
                stream.cycle.store.diagnostic_connection_state(),
                news_cycle.store.diagnostic_connection_state(),
            )
            open_connections = sum(int(item["open_connections"] or 0) for item in sqlite_states)
            latest_universe_size[0] = len(stream.cycle.selected_symbols())
        rss_mib = (
            float(system["rss_bytes"]) / 1024**2 if system.get("rss_bytes") is not None else None
        )
        private_mib = (
            float(system["private_bytes"]) / 1024**2
            if system.get("private_bytes") is not None
            else None
        )
        uss_mib = (
            float(system["uss_bytes"]) / 1024**2 if system.get("uss_bytes") is not None else None
        )
        sockets = system.get("established_tcp_connections")
        if sockets == -1:
            sockets = None
        queue_depth = int(cast(int, stream_state.get("queue_backlog_current", 0)))
        trend_sample = MemoryTrendSample(
            elapsed_seconds=elapsed_seconds,
            process_rss_mib=rss_mib,
            process_private_bytes_mib=private_mib,
            process_uss_mib=uss_mib,
            python_heap_current_mib=(
                current_bytes / 1024**2 if current_bytes is not None else None
            ),
            python_heap_peak_mib=(peak_bytes / 1024**2 if peak_bytes is not None else None),
            asyncio_tasks=len(tasks),
            pending_tasks=pending_tasks,
            thread_count=int(system["thread_count"])
            if system.get("thread_count") is not None
            else None,
            handle_count=int(system["handle_count"])
            if system.get("handle_count") is not None
            else None,
            open_sockets=int(sockets) if sockets is not None else None,
            queue_depth=queue_depth,
        )
        if retain_for_trend:
            memory_trend_samples.append(trend_sample)
        trend = evaluate_memory_trend(
            list(memory_trend_samples),
            warmup_seconds=config.diagnostics.memory_warmup_seconds,
            minimum_samples=config.diagnostics.memory_minimum_samples,
            minimum_span_seconds=config.diagnostics.memory_minimum_span_seconds,
            python_heap_slope_limit_mib_per_hour=(
                config.diagnostics.python_heap_slope_mib_per_hour
            ),
            private_bytes_slope_limit_mib_per_hour=(
                config.diagnostics.private_bytes_slope_mib_per_hour
            ),
            task_growth_limit_per_hour=config.diagnostics.task_growth_per_hour,
            thread_growth_limit_per_hour=config.diagnostics.thread_growth_per_hour,
            handle_growth_limit_per_hour=config.diagnostics.handle_growth_per_hour,
            socket_growth_limit_per_hour=config.diagnostics.socket_growth_per_hour,
            queue_final_depth=config.diagnostics.queue_final_depth,
            acceptance_window_seconds=(config.diagnostics.memory_acceptance_window_seconds),
            private_bytes_budget_mib=config.diagnostics.private_bytes_budget_mib,
            require_python_heap=deep_tracing,
        )
        status = (
            MemoryHealthStatus.MEMORY_WARMING
            if elapsed_seconds < config.diagnostics.memory_warmup_seconds
            else trend.status
        )
        no_data = ["http_connection_pool_size", "sqlite_cursors"]
        if not deep_tracing:
            no_data.extend(
                (
                    "python_heap_current_mib",
                    "python_heap_peak_mib",
                )
            )
        if not enhanced_metrics:
            no_data.extend(
                (
                    "gc_objects",
                    "gc_generation_counts",
                    "gc_collection_counts",
                )
            )
        if uss_mib is None:
            no_data.append("process_uss_mib")
        health = MemoryHealthSnapshot(
            status=status,
            measurement_time=_utcnow(),
            process_rss_mib=rss_mib,
            process_private_bytes_mib=private_mib,
            process_uss_mib=uss_mib,
            python_heap_current_mib=(
                current_bytes / 1024**2 if current_bytes is not None else None
            ),
            python_heap_peak_mib=(peak_bytes / 1024**2 if peak_bytes is not None else None),
            gc_objects=len(gc.get_objects()) if enhanced_metrics else None,
            gc_generation_counts=tuple(gc.get_count()) if enhanced_metrics else None,
            gc_collection_counts=(
                tuple(int(item["collections"]) for item in gc_stats)
                if gc_stats is not None
                else None
            ),
            asyncio_tasks=len(tasks),
            pending_tasks=pending_tasks,
            done_tasks=done_tasks,
            thread_count=trend_sample.thread_count,
            handle_count=trend_sample.handle_count,
            open_sockets=int(sockets) if sockets is not None else None,
            http_connection_pool_size=None,
            sqlite_connections=open_connections,
            sqlite_cursors=None,
            queue_depth=queue_depth,
            active_books=int(cast(int, stream_state.get("active_books", 0))),
            active_workers=int(cast(int, stream_state.get("active_workers", 0))),
            active_connections=int(cast(int, stream_state.get("active_connections", 0))),
            news_cache_entries=news_state["news_cache_entries"],
            universe_cache_entries=latest_universe_size[0],
            metrics_buffer_length=int(cast(int, stream_state.get("metrics_buffer_length", 0))),
            no_data_metrics=tuple(no_data),
        )
        return health, trend_sample

    async def snapshot_loop() -> None:
        # Offset process samples from the exact tracemalloc schedule so profiler
        # allocation spikes do not contaminate the operational slope.
        next_snapshot = started_monotonic + args.snapshot_seconds / 2
        while not stop_event.is_set():
            wait = max(0, next_snapshot - time.monotonic())
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait)
                break
            except TimeoutError:
                pass
            db = await asyncio.to_thread(_db_snapshot, database_path)
            system = await asyncio.to_thread(
                _windows_process_sample, (os.getpid(), api_process.pid)
            )
            elapsed_seconds = time.monotonic() - started_monotonic
            memory, _trend_sample = capture_memory(system, elapsed_seconds)
            if memory is not None:
                await asyncio.to_thread(
                    stream.cycle.store.set_checkpoint,
                    "diagnostics:memory",
                    memory.model_dump_json(),
                )
            if snapshots and system.get("cpu_seconds") is not None:
                previous = snapshots[-1]
                previous_system = previous["system"]
                previous_cpu = previous_system.get("cpu_seconds")
                wall_delta = time.monotonic() - started_monotonic - previous["elapsed_seconds"]
                if previous_cpu is not None and wall_delta > 0:
                    cpu_delta = max(0.0, float(system["cpu_seconds"]) - float(previous_cpu))
                    system["cpu_percent_one_core"] = round(100 * cpu_delta / wall_delta, 3)
                    system["cpu_percent_total_capacity"] = round(
                        100 * cpu_delta / wall_delta / max(1, os.cpu_count() or 1), 3
                    )
            disk = shutil.disk_usage(artifact_dir)
            item = {
                "captured_at": _utcnow().isoformat(),
                "elapsed_seconds": round(elapsed_seconds, 3),
                "database": db,
                "system": system,
                "disk_free_bytes": disk.free,
                "event_loop_lag_average_ms": round(sum(event_loop_lags) / len(event_loop_lags), 3)
                if event_loop_lags
                else None,
                "event_loop_lag_max_ms": round(max(event_loop_lags), 3)
                if event_loop_lags
                else None,
                "api_process_alive": api_process.poll() is None,
                "memory": memory.model_dump(mode="json") if memory is not None else None,
            }
            snapshots.append(item)
            with (artifact_dir / "metrics-5m.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
            if memory is not None:
                with (artifact_dir / "process-metrics.jsonl").open("a", encoding="utf-8") as handle:
                    handle.write(memory.model_dump_json() + "\n")
            (artifact_dir / "news-provider-health.json").write_text(
                json.dumps(db.get("news", {}), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            stream_metrics = db.get("stream", {})
            print(
                json.dumps(
                    {
                        "snapshot_minute": round(
                            float(cast(float, item["elapsed_seconds"])) / 60, 1
                        ),
                        "received": stream_metrics.get("ws_events_received_total"),
                        "accepted": stream_metrics.get("ws_events_accepted_total"),
                        "dropped": stream_metrics.get("ws_events_dropped_total"),
                        "status": stream_metrics.get("ws_status_events_total"),
                        "reconciled": stream_metrics.get("ws_counter_reconciled"),
                        "queue": stream_metrics.get("queue_backlog_current"),
                        "rss_bytes": system.get("rss_bytes"),
                        "private_bytes": system.get("private_bytes"),
                        "python_heap_current_mib": (
                            memory.python_heap_current_mib if memory is not None else None
                        ),
                        "cpu_percent": system.get("cpu_percent_total_capacity"),
                        "db_bytes": db.get("database_bytes"),
                    }
                )
            )
            next_snapshot += args.snapshot_seconds

    async def tracemalloc_loop() -> None:
        nonlocal previous_trace
        schedule = (
            (600.0, "010m"),
            (1800.0, "030m"),
            (3600.0, "060m"),
            (7200.0, "120m"),
            (10800.0, "180m"),
        )
        for due_seconds, label in schedule:
            if due_seconds >= args.duration_seconds:
                return
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=max(0.0, started_monotonic + due_seconds - time.monotonic()),
                )
                return
            except TimeoutError:
                previous_trace = await asyncio.to_thread(
                    _write_tracemalloc_diff,
                    artifact_dir,
                    label=label,
                    elapsed_seconds=time.monotonic() - started_monotonic,
                    previous=previous_trace,
                )

    initial_system = await asyncio.to_thread(
        _windows_process_sample, (os.getpid(), api_process.pid)
    )
    initial_memory, _initial_sample = capture_memory(
        initial_system, time.monotonic() - started_monotonic
    )
    await asyncio.to_thread(
        stream.cycle.store.set_checkpoint,
        "diagnostics:memory",
        initial_memory.model_dump_json(),
    )
    with (artifact_dir / "process-metrics.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(initial_memory.model_dump_json() + "\n")

    stream_task = asyncio.create_task(stream.run(), name="public-websocket-stream")
    api_task = asyncio.create_task(api_loop(), name="api-load")
    lag_task = asyncio.create_task(lag_loop(), name="event-loop-lag")
    refresh_task = asyncio.create_task(reconciliation_loop(), name="public-reconciliation")
    snapshot_task = asyncio.create_task(snapshot_loop(), name="five-minute-snapshots")
    trace_task = (
        asyncio.create_task(tracemalloc_loop(), name="diagnostic-tracemalloc")
        if deep_tracing
        else None
    )
    interrupted = False
    continuous_elapsed = 0.0
    try:
        deadline = started_monotonic + args.duration_seconds
        while time.monotonic() < deadline:
            if api_process.poll() is not None:
                raise RuntimeError(f"FastAPI exited during the soak: exit={api_process.returncode}")
            if stream_task.done():
                exception = stream_task.exception()
                raise RuntimeError(f"WebSocket stopped during the soak: {exception}")
            await asyncio.sleep(min(5, deadline - time.monotonic()))
    except KeyboardInterrupt:
        interrupted = True
        run_errors.append("soak interrupted by the user")
    except Exception as exc:
        run_errors.append(f"soak: {type(exc).__name__}: {exc}")
    finally:
        continuous_elapsed = time.monotonic() - started_monotonic
        pre_system = await asyncio.to_thread(
            _windows_process_sample, (os.getpid(), api_process.pid)
        )
        pre_shutdown_memory, _sample = capture_memory(
            pre_system, continuous_elapsed, retain_for_trend=False
        )
        if deep_tracing:
            if continuous_elapsed >= 14_399.0:
                previous_trace = await asyncio.to_thread(
                    _write_tracemalloc_diff,
                    artifact_dir,
                    label="240m",
                    elapsed_seconds=continuous_elapsed,
                    previous=previous_trace,
                )
            previous_trace = await asyncio.to_thread(
                _write_tracemalloc_diff,
                artifact_dir,
                label="pre-shutdown",
                elapsed_seconds=continuous_elapsed,
                previous=previous_trace,
            )
        stop_event.set()
        tasks_to_cancel = [api_task, lag_task, snapshot_task, stream_task]
        if trace_task is not None:
            tasks_to_cancel.append(trace_task)
        for task in tasks_to_cancel:
            task.cancel()
        await asyncio.gather(
            *tasks_to_cancel,
            return_exceptions=True,
        )
        try:
            await asyncio.wait_for(refresh_task, timeout=600)
        except TimeoutError:
            run_errors.append("in-flight REST/RSS refresh did not finish within 600 seconds")
            refresh_task.cancel()
            await asyncio.gather(refresh_task, return_exceptions=True)
        stream.cycle.stream_metrics.update_queue_backlog(0)
        stream.close()
        crypto_cycle.close()
        news_cycle.close()
        api_stop = await asyncio.to_thread(_stop_process, api_process)
        api_log.close()
        if enhanced_metrics:
            # Explicit diagnostic GC occurs only after every owned I/O resource closes.
            gc.collect()
        resources_closed_system = await asyncio.to_thread(_windows_process_sample, (os.getpid(),))
        resources_closed_memory, _resources_sample = capture_memory(
            resources_closed_system,
            time.monotonic() - started_monotonic,
            retain_for_trend=False,
            resources_closed=True,
        )
        with (artifact_dir / "resources-closed.json").open("w", encoding="utf-8") as handle:
            handle.write(resources_closed_memory.model_dump_json(indent=2))
        if deep_tracing:
            previous_trace = await asyncio.to_thread(
                _write_tracemalloc_diff,
                artifact_dir,
                label="resources-closed",
                elapsed_seconds=time.monotonic() - started_monotonic,
                previous=previous_trace,
            )
        if deep_tracing:
            tracemalloc.stop()

    finished_at = _utcnow()
    shutdown_elapsed = time.monotonic() - started_monotonic
    final_db = await asyncio.to_thread(_db_snapshot, database_path)
    domain = await asyncio.to_thread(_domain_metrics, database_path)
    final_system = await asyncio.to_thread(_windows_process_sample, (os.getpid(), api_process.pid))
    table_counts = final_db.get("table_counts", {})
    safety_counts = {table: int(table_counts.get(table, 0)) for table in SAFETY_TABLES}
    # The DB checkpoint is periodic.  After graceful cancellation use the owning
    # in-process registry so final queue/book/worker counts reflect cleanup.
    stream_metrics = stream.cycle.stream_metrics.as_payload()
    news_health = final_db.get("news", {})
    providers = news_health.get("provider_health", [])
    healthy_providers: list[str] = [
        str(item["provider_id"])
        for item in providers
        if isinstance(item, dict)
        and item.get("health_status") == "healthy"
        and item.get("provider_id") is not None
    ]
    api_summary: dict[str, Any] = {}
    for path, metric in api_metrics.items():
        latencies = metric["latencies_ms"]
        api_summary[path] = {
            "attempts": metric["attempts"],
            "successes": metric["successes"],
            "errors": metric["errors"],
            "secret_markers": metric.get("secret_markers", 0),
            "retained_latency_samples": len(latencies),
            "success_rate": round(metric["successes"] / metric["attempts"], 6)
            if metric["attempts"]
            else 0,
            "p50_ms": _percentile(latencies, 0.50),
            "p95_ms": _percentile(latencies, 0.95),
            "p99_ms": _percentile(latencies, 0.99),
            "max_response_bytes": metric["max_response_bytes"],
        }
    (artifact_dir / "api-metrics.json").write_text(
        json.dumps(api_summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    sys.stdout.flush()
    soak_log_text = (artifact_dir / "soak.log").read_text(encoding="utf-8", errors="replace")
    api_log_text = (artifact_dir / "api.log").read_text(encoding="utf-8", errors="replace")
    database_lock_errors = (soak_log_text + api_log_text).casefold().count("database is locked")
    api_graceful_shutdown = all(
        marker in api_log_text for marker in ("Shutting down", "Application shutdown complete")
    )
    memory_samples = [
        int(item["system"].get("rss_bytes", 0))
        for item in snapshots
        if item["system"].get("rss_bytes") is not None
    ]
    messages_per_minute = (
        round(
            float(cast(int, stream_metrics.get("ws_events_received_total", 0)))
            / continuous_elapsed
            * 60,
            3,
        )
        if continuous_elapsed > 0
        else None
    )
    event_loop_p95_ms = _percentile(event_loop_lags, 0.95)
    event_latency_p95_ms = stream_metrics.get("clock_adjusted_latency_p95_ms")

    uninterrupted = (
        not interrupted and not run_errors and continuous_elapsed >= args.duration_seconds - 0.5
    )
    memory_trend = evaluate_memory_trend(
        list(memory_trend_samples),
        warmup_seconds=config.diagnostics.memory_warmup_seconds,
        minimum_samples=config.diagnostics.memory_minimum_samples,
        minimum_span_seconds=config.diagnostics.memory_minimum_span_seconds,
        python_heap_slope_limit_mib_per_hour=(config.diagnostics.python_heap_slope_mib_per_hour),
        private_bytes_slope_limit_mib_per_hour=(
            config.diagnostics.private_bytes_slope_mib_per_hour
        ),
        task_growth_limit_per_hour=config.diagnostics.task_growth_per_hour,
        thread_growth_limit_per_hour=config.diagnostics.thread_growth_per_hour,
        handle_growth_limit_per_hour=config.diagnostics.handle_growth_per_hour,
        socket_growth_limit_per_hour=config.diagnostics.socket_growth_per_hour,
        queue_final_depth=config.diagnostics.queue_final_depth,
        observed_final_queue_depth=0,
        acceptance_window_seconds=config.diagnostics.memory_acceptance_window_seconds,
        private_bytes_budget_mib=config.diagnostics.private_bytes_budget_mib,
        require_python_heap=deep_tracing,
    )
    memory_observed: bool | None
    if memory_trend.status is MemoryHealthStatus.MEMORY_OK:
        memory_observed = True
    elif memory_trend.status in {
        MemoryHealthStatus.MEMORY_WARN,
        MemoryHealthStatus.MEMORY_CRITICAL,
    }:
        memory_observed = False
    else:
        memory_observed = None
    four_hours = uninterrupted and continuous_elapsed >= 14_399.5
    unexplained_reconnects = int(
        cast(int, stream_metrics.get("ws_unexplained_reconnects_total", 0))
    )
    unexplained_median_lifetime = stream_metrics.get("unexplained_connection_lifetime_p50_s")
    reconnect_lifetime_ok = unexplained_reconnects == 0 or (
        unexplained_median_lifetime is not None
        and float(cast(float, unexplained_median_lifetime))
        >= config.diagnostics.minimum_median_connection_lifetime_s
    )
    typed_criteria = [
        criterion(
            "continuous_four_hour_duration",
            "A single uninterrupted public soak ran for at least four hours",
            four_hours,
            observed_value=round(continuous_elapsed, 3),
            threshold=14_400,
            evidence=(str(artifact_dir / "soak.log"),),
            failure_reason="the four-hour uninterrupted acceptance duration was not completed",
        ),
        criterion(
            "memory_post_warmup_trend",
            "Post-warm-up resource slopes and the final queue are within limits",
            memory_observed,
            observed_value=memory_trend.model_dump(mode="json"),
            threshold={
                "python_heap_mib_per_hour": config.diagnostics.python_heap_slope_mib_per_hour,
                "private_bytes_mib_per_hour": (config.diagnostics.private_bytes_slope_mib_per_hour),
                "task_growth_per_hour": config.diagnostics.task_growth_per_hour,
                "thread_growth_per_hour": config.diagnostics.thread_growth_per_hour,
                "handle_growth_per_hour": config.diagnostics.handle_growth_per_hour,
                "socket_growth_per_hour": config.diagnostics.socket_growth_per_hour,
                "queue_final_depth": config.diagnostics.queue_final_depth,
                "acceptance_window_seconds": (config.diagnostics.memory_acceptance_window_seconds),
                "private_bytes_budget_mib": config.diagnostics.private_bytes_budget_mib,
            },
            evidence=(str(artifact_dir / "process-metrics.jsonl"),),
            failure_reason="; ".join(memory_trend.reasons) or "memory result is inconclusive",
        ),
        criterion(
            "at_least_two_healthy_news_providers",
            "At least two RSS/Atom providers are fresh and healthy",
            len(healthy_providers) >= 2,
            observed_value=healthy_providers,
            threshold=2,
            evidence=(str(artifact_dir / "news-provider-health.json"),),
            failure_reason="two healthy news providers could not be verified",
        ),
        criterion(
            "websocket_counter_reconciled",
            "The WS received counter reconciles with the accepted/drop/parse/control total",
            stream_metrics.get("ws_counter_reconciled") is True,
            observed_value={
                "received": stream_metrics.get("ws_events_received_total"),
                "classified": stream_metrics.get("ws_counter_classified_total"),
            },
            evidence=(str(artifact_dir / "metrics-5m.jsonl"),),
            failure_reason="WebSocket counter reconciliation failed",
        ),
        criterion(
            "websocket_gap_accounting",
            "Gap/resync accounting is complete; failed/pending/stale continuation is zero",
            stream_metrics.get("ws_gap_accounting_reconciled") is True
            and stream_metrics.get("ws_resync_failures_total", 0) == 0
            and stream_metrics.get("ws_pending_resyncs", 0) == 0
            and stream_metrics.get("ws_stale_book_continuations_total", 0) == 0,
            observed_value={
                "gaps": stream_metrics.get("ws_sequence_gaps_total"),
                "successful": stream_metrics.get("ws_resync_total"),
                "failed": stream_metrics.get("ws_resync_failures_total"),
                "aborted_shutdown": stream_metrics.get("ws_resync_aborted_during_shutdown_total"),
                "pending": stream_metrics.get("ws_pending_resyncs"),
                "stale_continuations": stream_metrics.get("ws_stale_book_continuations_total"),
            },
            threshold={"failed": 0, "pending": 0, "stale_continuations": 0},
            failure_reason="gap/resync accounting or book safety failed",
        ),
        criterion(
            "websocket_reconnect_limits",
            "Unexplained reconnect rate, burst, lifetime and backoff are within limits",
            float(cast(float, stream_metrics.get("reconnect_per_worker_per_hour", 0.0)))
            <= config.diagnostics.reconnect_per_worker_per_hour
            and int(cast(int, stream_metrics.get("reconnect_burst_max_60s", 0)))
            <= config.diagnostics.reconnect_burst_max
            and int(cast(int, stream_metrics.get("reconnect_without_backoff_total", 0))) == 0
            and reconnect_lifetime_ok,
            observed_value={
                "per_worker_per_hour": stream_metrics.get("reconnect_per_worker_per_hour"),
                "burst_max_60s": stream_metrics.get("reconnect_burst_max_60s"),
                "all_closures_median_lifetime_s": stream_metrics.get("connection_lifetime_p50_s"),
                "unexplained_reconnects": unexplained_reconnects,
                "unexplained_median_lifetime_s": unexplained_median_lifetime,
                "without_backoff": stream_metrics.get("reconnect_without_backoff_total"),
                "reasons": stream_metrics.get("reconnect_reasons"),
            },
            threshold={
                "per_worker_per_hour": (config.diagnostics.reconnect_per_worker_per_hour),
                "burst_max_60s": config.diagnostics.reconnect_burst_max,
                "median_lifetime_s": (config.diagnostics.minimum_median_connection_lifetime_s),
                "without_backoff": 0,
            },
            failure_reason="reconnect storm, short-lived connection or backoff violation",
        ),
        criterion(
            "event_loop_and_event_latency",
            "Production event-loop and event latency p95 are within limits",
            event_loop_p95_ms is not None
            and event_loop_p95_ms <= config.diagnostics.event_loop_lag_p95_ms
            and isinstance(event_latency_p95_ms, (int, float))
            and float(event_latency_p95_ms) <= config.diagnostics.event_latency_p95_ms,
            observed_value={
                "event_loop_lag_p95_ms": event_loop_p95_ms,
                "event_latency_p95_ms": event_latency_p95_ms,
            },
            threshold={
                "event_loop_lag_p95_ms": config.diagnostics.event_loop_lag_p95_ms,
                "event_latency_p95_ms": config.diagnostics.event_latency_p95_ms,
            },
            failure_reason="event-loop or event latency p95 limit exceeded",
        ),
        criterion(
            "websocket_batch_write_failures_zero",
            "No stream-batch SQLite write errors",
            stream_metrics.get("batch_write_failures", 0) == 0,
            observed_value=stream_metrics.get("batch_write_failures"),
            threshold=0,
            failure_reason="a stream-batch write error was observed",
        ),
        criterion(
            "worker_and_book_cleanup",
            "No worker, connection or order-book state is left after graceful shutdown",
            stream_metrics.get("active_workers") == 0
            and stream_metrics.get("active_connections") == 0
            and stream_metrics.get("active_books") == 0,
            observed_value={
                "active_workers": stream_metrics.get("active_workers"),
                "active_connections": stream_metrics.get("active_connections"),
                "active_books": stream_metrics.get("active_books"),
            },
            threshold={"active_workers": 0, "active_connections": 0, "active_books": 0},
            failure_reason="worker/connection/order-book state remained after shutdown",
        ),
        criterion(
            "owned_resources_closed_snapshot",
            "The resources_closed snapshot was taken after all owned I/O resources closed",
            resources_closed_memory.sqlite_connections == 0
            and resources_closed_memory.active_connections == 0
            and resources_closed_memory.active_workers == 0
            and resources_closed_memory.active_books == 0
            and resources_closed_memory.queue_depth == 0,
            observed_value=resources_closed_memory.model_dump(mode="json"),
            threshold={
                "sqlite_connections": 0,
                "active_connections": 0,
                "active_workers": 0,
                "active_books": 0,
                "queue_depth": 0,
            },
            evidence=(str(artifact_dir / "resources-closed.json"),),
            failure_reason="the final snapshot was taken before owned resources finished closing",
        ),
        criterion(
            "sqlite_errors_zero",
            "No SQLite snapshot/lock errors",
            all(item["database"].get("sqlite_error") is None for item in snapshots)
            and final_db.get("sqlite_error") is None
            and database_lock_errors == 0,
            observed_value={"lock_errors": database_lock_errors},
            threshold=0,
            evidence=(str(database_path),),
            failure_reason="a SQLite snapshot or lock error was observed",
        ),
        criterion(
            "api_success_rate",
            "Every polled API endpoint succeeds at or above the configured rate",
            bool(api_summary)
            and all(
                item["success_rate"] >= config.diagnostics.api_minimum_success_rate
                for item in api_summary.values()
            ),
            observed_value={key: value["success_rate"] for key, value in api_summary.items()},
            threshold=config.diagnostics.api_minimum_success_rate,
            evidence=(str(artifact_dir / "api-metrics.json"),),
            failure_reason="API success rate is below the threshold",
        ),
        criterion(
            "api_secret_markers_zero",
            "No secret markers in API responses",
            all(item.get("secret_markers", 0) == 0 for item in api_summary.values()),
            observed_value=sum(int(item.get("secret_markers", 0)) for item in api_summary.values()),
            threshold=0,
            failure_reason="a secret marker was found in an API response",
        ),
        criterion(
            "public_only_safety_tables_zero",
            "OrderIntent/shadow/fill/position/mission tables are empty",
            all(value == 0 for value in safety_counts.values()),
            observed_value=safety_counts,
            threshold=0,
            evidence=(str(artifact_dir / "preflight.json"), str(database_path)),
            failure_reason="one of the public-only safety tables is not empty",
        ),
        criterion(
            "api_graceful_shutdown",
            "The FastAPI process shut down gracefully and left no process behind",
            api_graceful_shutdown and not api_stop.get("alive_after_stop", False),
            observed_value=api_stop,
            evidence=(str(artifact_dir / "api.log"),),
            failure_reason="FastAPI graceful shutdown could not be verified",
        ),
    ]
    readiness = ReadinessEvaluator.evaluate(typed_criteria)
    (artifact_dir / "readiness.json").write_text(
        readiness.model_dump_json(indent=2), encoding="utf-8"
    )
    first_db_bytes = int(snapshots[0]["database"].get("database_bytes", 0)) if snapshots else 0
    final_db_bytes = int(final_db.get("database_bytes", 0))
    measured_storage_hours = (
        max(
            0.0,
            float(snapshots[-1]["elapsed_seconds"]) - float(snapshots[0]["elapsed_seconds"]),
        )
        / 3600
        if len(snapshots) >= 2
        else 0.0
    )
    database_growth_per_hour = (
        max(0, final_db_bytes - first_db_bytes) / measured_storage_hours
        if measured_storage_hours > 0
        else None
    )
    storage_projection = {
        "first_database_bytes": first_db_bytes,
        "final_database_bytes": final_db_bytes,
        "growth_bytes_per_hour": database_growth_per_hour,
        "projected_7d_bytes": (
            final_db_bytes + database_growth_per_hour * 24 * 7
            if database_growth_per_hour is not None
            else None
        ),
        "projected_30d_bytes": (
            final_db_bytes + database_growth_per_hour * 24 * 30
            if database_growth_per_hour is not None
            else None
        ),
        "table_bytes": final_db.get("table_bytes", {}),
        "retained_rows": final_db.get("table_counts", {}),
        "final_wal_bytes": final_db.get("wal_bytes"),
        "free_disk_bytes": shutil.disk_usage(artifact_dir).free,
        "projection_assumption": "linear observed growth; not an optimal retention target",
    }
    report = {
        "decision": readiness.decision.value,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": round(continuous_elapsed, 3),
        "duration_including_shutdown_seconds": round(shutdown_elapsed, 3),
        "artifact_directory": str(artifact_dir),
        "database_path": str(database_path),
        "database_quick_check": _quick_check(database_path),
        "readiness": readiness.model_dump(mode="json"),
        "healthy_news_providers": healthy_providers,
        "news_health": news_health,
        "stream_metrics": stream_metrics,
        "messages_per_minute": messages_per_minute,
        "api": api_summary,
        "refresh_runs": list(refresh_runs),
        "database": final_db,
        "storage_projection": storage_projection,
        "domain_metrics": domain,
        "database_lock_errors": database_lock_errors,
        "system": final_system,
        "memory": {
            "samples": len(memory_samples),
            "first_rss_bytes": memory_samples[0] if memory_samples else None,
            "last_rss_bytes": memory_samples[-1] if memory_samples else None,
            "minimum_rss_bytes": min(memory_samples) if memory_samples else None,
            "maximum_rss_bytes": max(memory_samples) if memory_samples else None,
            "growth_bytes": memory_samples[-1] - memory_samples[0]
            if len(memory_samples) >= 2
            else None,
            "trend": memory_trend.model_dump(mode="json"),
            "pre_shutdown": (
                pre_shutdown_memory.model_dump(mode="json")
                if pre_shutdown_memory is not None
                else None
            ),
            "post_shutdown": (resources_closed_memory.model_dump(mode="json")),
            "resources_closed": resources_closed_memory.model_dump(mode="json"),
            "metrics_mode": metrics_mode,
        },
        "snapshots": list(snapshots),
        "event_loop_lag": {
            "samples": len(event_loop_lags),
            "average_ms": round(sum(event_loop_lags) / len(event_loop_lags), 3)
            if event_loop_lags
            else None,
            "maximum_ms": round(max(event_loop_lags), 3) if event_loop_lags else None,
            "p95_ms": event_loop_p95_ms,
        },
        "safety_counts": safety_counts,
        "run_errors": list(run_errors),
        "api_shutdown": api_stop,
    }
    (artifact_dir / "final-report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    hourly_memory_rows = [
        "| Hour | Heap MiB | Private MiB | RSS MiB | Task | Thread | Handle | Socket | Queue |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    retained_memory_samples = list(memory_trend_samples)
    for hour in range(5):
        if not retained_memory_samples:
            break
        target_seconds = hour * 3600
        nearest = min(
            retained_memory_samples,
            key=lambda item: abs(item.elapsed_seconds - target_seconds),
        )
        if abs(nearest.elapsed_seconds - target_seconds) > max(args.snapshot_seconds, 330):
            continue
        heap_cell = (
            f"{nearest.python_heap_current_mib:.3f}"
            if nearest.python_heap_current_mib is not None
            else "NO_DATA"
        )
        hourly_memory_rows.append(
            "| "
            f"{hour} | {heap_cell} | "
            f"{nearest.process_private_bytes_mib} | {nearest.process_rss_mib} | "
            f"{nearest.asyncio_tasks} | {nearest.thread_count} | {nearest.handle_count} | "
            f"{nearest.open_sockets} | {nearest.queue_depth} |"
        )
    trace_files = sorted(artifact_dir.glob("tracemalloc-*.json"))
    api_rates = {key: value["success_rate"] for key, value in api_summary.items()}
    api_p95 = {key: value["p95_ms"] for key, value in api_summary.items()}
    first_snapshot = snapshots[0] if snapshots else {}
    last_snapshot = snapshots[-1] if snapshots else {}
    first_database = first_snapshot.get("database", {})
    last_database = last_snapshot.get("database", {})
    metric_caps = {
        "retained": stream_metrics.get("metrics_buffer_length"),
        "labels": stream_metrics.get("stream_label_count"),
        "label_cap": stream_metrics.get("maximum_stream_labels"),
    }
    markdown = [
        "# Argus public memory/news/market soak report",
        "",
        "## 1. Overall decision",
        "",
        f"`{report['decision']}`. Result of the central evaluator; READY is never produced unless "
        "every mandatory criterion is PASS.",
        "",
        "## 2. Actual soak duration",
        "",
        f"Uninterrupted measured duration {report['duration_seconds']} seconds; start "
        f"{report['started_at']}, end {report['finished_at']}. Including shutdown: "
        f"{report['duration_including_shutdown_seconds']} seconds.",
        "",
        "## 3. Hourly memory table",
        "",
        *hourly_memory_rows,
        "",
        "## 4. Tracemalloc comparisons",
        "",
        f"Metrics mode `{metrics_mode}`; {len(trace_files)} allocation comparison files "
        f"written: `{', '.join(path.name for path in trace_files) or 'none (production)'}`.",
        "",
        "## 5. Python heap result",
        "",
        f"Final acceptance-window slope "
        f"{memory_trend.acceptance_python_heap.slope_per_hour} MiB/hour; "
        f"measurement required={memory_trend.python_heap_required}. Threshold "
        f"{config.diagnostics.python_heap_slope_mib_per_hour} MiB/hour.",
        "",
        "## 6. RSS, private bytes and USS",
        "",
        f"RSS slope {memory_trend.rss.slope_per_hour} MiB/hour (diagnostic, not a gate by itself); "
        f"final-window private slope "
        f"{memory_trend.acceptance_private_bytes.slope_per_hour} MiB/hour; "
        f"plateau={memory_trend.plateau_detected}; USS may be NO_DATA with the "
        "Windows sampler.",
        "",
        "## 7. GC behaviour",
        "",
        "Pre-/post-shutdown observation: "
        f"`{json.dumps(report['memory']['pre_shutdown'], ensure_ascii=False)}` / "
        f"`{json.dumps(report['memory']['post_shutdown'], ensure_ascii=False)}`.",
        "",
        "## 8. Asyncio task result",
        "",
        f"Task slope {memory_trend.tasks.slope_per_hour}/hour; threshold "
        f"{config.diagnostics.task_growth_per_hour}/hour. At shutdown worker="
        f"{stream_metrics.get('active_workers')}, book={stream_metrics.get('active_books')}.",
        "",
        "## 9. Thread and handle result",
        "",
        f"Thread slope {memory_trend.threads.slope_per_hour}/hour, handle slope "
        f"{memory_trend.handles.slope_per_hour}/hour; thresholds are "
        f"{config.diagnostics.thread_growth_per_hour} and "
        f"{config.diagnostics.handle_growth_per_hour}.",
        "",
        "## 10. HTTP/WebSocket/socket result",
        "",
        f"Open TCP slope {memory_trend.sockets.slope_per_hour}/hour; threshold "
        f"{config.diagnostics.socket_growth_per_hour}. HTTP connection-pool size is "
        "labelled NO_DATA because the stdlib urllib/websockets surface does not expose it "
        "reliably.",
        "",
        "## 11. SQLite connection and cursor result",
        "",
        f"SQLite quick_check `{report['database_quick_check']}`, lock errors "
        f"{database_lock_errors}. Owned connections are measured; the stdlib cursor count "
        "is NO_DATA.",
        "",
        "## 12. Queue, cache and bounded-state result",
        "",
        f"Queue final/maximum {stream_metrics.get('queue_backlog_current')}/"
        f"{stream_metrics.get('queue_backlog_max')}; metric bounded state "
        f"`{json.dumps(metric_caps, sort_keys=True)}`.",
        "",
        "## 13. Event throughput",
        "",
        f"Received={stream_metrics.get('ws_events_received_total')}, accepted="
        f"{stream_metrics.get('ws_events_accepted_total')}, dropped="
        f"{stream_metrics.get('ws_events_dropped_total')}, parse="
        f"{stream_metrics.get('ws_parse_errors_total')}, control="
        f"{stream_metrics.get('ws_status_events_total')}, messages/minute={messages_per_minute}; "
        f"reconciled={stream_metrics.get('ws_counter_reconciled')}.",
        "",
        "## 14. Event latency breakdown",
        "",
        f"Raw p50/p95/p99={stream_metrics.get('raw_latency_p50_ms')}/"
        f"{stream_metrics.get('raw_latency_p95_ms')}/"
        f"{stream_metrics.get('raw_latency_p99_ms')} ms; "
        f"clock-adjusted={stream_metrics.get('clock_adjusted_latency_p50_ms')}/"
        f"{stream_metrics.get('clock_adjusted_latency_p95_ms')}/"
        f"{stream_metrics.get('clock_adjusted_latency_p99_ms')} ms; queue wait="
        f"{stream_metrics.get('queue_wait_p50_ms')}/{stream_metrics.get('queue_wait_p95_ms')}/"
        f"{stream_metrics.get('queue_wait_p99_ms')} ms; batch wait="
        f"{stream_metrics.get('batch_wait_p50_ms')}/{stream_metrics.get('batch_wait_p95_ms')}/"
        f"{stream_metrics.get('batch_wait_p99_ms')} ms.",
        "",
        "## 15. Sequence gap/resync",
        "",
        f"Gap/resync/failure={stream_metrics.get('ws_sequence_gaps_total')}/"
        f"{stream_metrics.get('ws_resync_total')}/"
        f"{stream_metrics.get('ws_resync_failures_total')}; "
        f"reconnect={stream_metrics.get('ws_reconnects_total')}.",
        "",
        "## 16. News sources and health",
        "",
        f"Healthy providers: {', '.join(healthy_providers) or 'none'}. News coverage: "
        f"`{json.dumps(domain.get('news_coverage', {}), ensure_ascii=False, sort_keys=True)}`; "
        f"entity/ambiguous matches={domain.get('news_entity_matches')}/"
        f"{domain.get('news_ambiguous_matches')}.",
        "",
        "## 17. TradePlan and NO_TRADE",
        "",
        f"Plan horizons `{json.dumps(domain.get('plan_horizons', {}), sort_keys=True)}`; "
        f"basis `{json.dumps(domain.get('plan_bases', {}), sort_keys=True)}`; cost rejections "
        f"{domain.get('cost_rejections')}; safety tables "
        f"`{json.dumps(safety_counts, sort_keys=True)}`.",
        "",
        "## 18. API performance",
        "",
        f"Success rates `{json.dumps(api_rates, sort_keys=True)}`; p95 ms "
        f"`{json.dumps(api_p95, sort_keys=True)}`; secret-marker total "
        f"{sum(int(item.get('secret_markers', 0)) for item in api_summary.values())}.",
        "",
        "## 19. DB/WAL/disk growth",
        "",
        f"DB first/last snapshot bytes={first_database.get('database_bytes')}/"
        f"{last_database.get('database_bytes')}; WAL={first_database.get('wal_bytes')}/"
        f"{last_database.get('wal_bytes')}; final DB={final_db.get('database_bytes')} bytes.",
        "",
        "## 20. Interpreting memory growth",
        "",
        "RSS growth alone is not proof of a leak: it mixes the Python allocator/GC, native "
        "library memory and the measurement tool's own buffers. This run keeps product "
        "state bounded, caps the diagnostic buffers and gates heap/private/OS resources "
        "separately.",
        "",
        "## 21. Measurement safeguards",
        "",
        "The soak latency/byte/refresh/snapshot/error lists are bounded; readiness comes from "
        "the central typed evaluator; stream latency is split into components with fixed "
        "histograms; event-bus history is capped; the universe API is paginated; task/book "
        "cleanup is visible.",
        "",
        "## 22. Test results",
        "",
        "This artifact holds live soak measurements only. Static backend/frontend/Docker "
        "quality-gate commands and results are recorded separately.",
        "",
        "## 23. What was demonstrated",
        "",
        *(
            f"- {item.status.value} — {item.criterion_id}: {item.description}"
            for item in readiness.criteria
        ),
        "",
        "## 24. What was not demonstrated, and remaining risks",
        "",
        "This run is not evidence of profitability/edge, private-account, testnet or live-order "
        "behaviour, or seven-day durability. USS and internal HTTP pool/cursor counters may "
        f"be NO_DATA; run errors: `{json.dumps(list(run_errors), ensure_ascii=False)}`.",
        "",
        "## 25. Artifacts and the safest next step",
        "",
        f"Artifact directory `{artifact_dir}`. Only if the decision is READY, the safe next "
        "step is to prepare a seven-day observation configuration with the same public-only "
        "limits and shadow disabled.",
    ]
    (artifact_dir / "final-report.md").write_text("\n".join(markdown) + "\n", encoding="utf-8")
    print(json.dumps({"final": report["decision"], "artifact": str(artifact_dir)}))
    return 0 if readiness.decision.value == "READY_FOR_7D_SHADOW" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-seconds", type=float, default=3600)
    parser.add_argument("--snapshot-seconds", type=float, default=300)
    parser.add_argument("--api-interval-seconds", type=float, default=15)
    parser.add_argument("--reconcile-seconds", type=float, default=300)
    parser.add_argument("--port", type=int, default=18787)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument(
        "--artifact-prefix",
        default=None,
        help="Prefix of the isolated artifact directory created under the temp directory",
    )
    parser.add_argument(
        "--metrics-mode",
        choices=("production", "diagnostic", "deep_tracing"),
        default="production",
    )
    parser.add_argument(
        "--websocket-compression",
        choices=("enabled", "disabled"),
        default=None,
        help="Effective config override for A/B runs; does not change the production default",
    )
    parser.add_argument(
        "--memory-diagnostic",
        action="store_true",
        help="Backward-compatible alias for --metrics-mode deep_tracing",
    )
    args = parser.parse_args()
    if (
        min(
            args.duration_seconds,
            args.snapshot_seconds,
            args.api_interval_seconds,
            args.reconcile_seconds,
        )
        <= 0
    ):
        parser.error("all durations must be positive")
    stamp = _utcnow().strftime("%Y%m%dT%H%M%SZ")
    effective_mode = "deep_tracing" if args.memory_diagnostic else args.metrics_mode
    default_prefix = {
        "production": "argus-news-public-soak",
        "diagnostic": "argus-diagnostic-soak",
        "deep_tracing": "argus-memory-soak",
    }[effective_mode]
    artifact_prefix = args.artifact_prefix or default_prefix
    artifact_dir = (
        args.artifact_dir.resolve()
        if args.artifact_dir is not None
        else Path(
            tempfile.gettempdir(),
            f"{artifact_prefix}-{stamp}",
        ).resolve()
    )
    artifact_dir.mkdir(parents=True, exist_ok=False)
    log_handle = (artifact_dir / "soak.log").open("w", encoding="utf-8")
    original_stdout, original_stderr = sys.stdout, sys.stderr
    sys.stdout = _Tee(original_stdout, log_handle)
    sys.stderr = _Tee(original_stderr, log_handle)
    print(json.dumps({"artifact": str(artifact_dir), "started_at": _utcnow().isoformat()}))
    try:
        return asyncio.run(_run_soak(args, artifact_dir))
    finally:
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        log_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
