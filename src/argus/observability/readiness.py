"""Typed, fail-closed readiness and long-running memory trend evaluation.

The evaluator is deliberately independent from the soak harness.  JSON, Markdown and
an independent reviewer consume the same :class:`ReadinessReport`; no caller may infer
``READY`` from a partial dictionary of booleans.
"""

from __future__ import annotations

import math
from datetime import datetime
from enum import StrEnum
from statistics import median
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CriterionStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_RUN = "NOT_RUN"


class ReadinessDecision(StrEnum):
    NOT_READY = "NOT_READY"
    READY_FOR_7D_SHADOW = "READY_FOR_7D_SHADOW"


class MemoryHealthStatus(StrEnum):
    MEMORY_OK = "MEMORY_OK"
    MEMORY_WARMING = "MEMORY_WARMING"
    MEMORY_WARN = "MEMORY_WARN"
    MEMORY_CRITICAL = "MEMORY_CRITICAL"
    MEMORY_INCONCLUSIVE = "MEMORY_INCONCLUSIVE"


class ReadinessCriterion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    criterion_id: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    required: bool = True
    status: CriterionStatus
    observed_value: Any = None
    threshold: Any = None
    evidence: tuple[str, ...] = ()
    failure_reason: str | None = None


class ReadinessReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: ReadinessDecision
    criteria: tuple[ReadinessCriterion, ...]


class MemoryTrendSample(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    elapsed_seconds: float = Field(..., ge=0)
    process_rss_mib: float | None = Field(default=None, ge=0)
    process_private_bytes_mib: float | None = Field(default=None, ge=0)
    process_uss_mib: float | None = Field(default=None, ge=0)
    python_heap_current_mib: float | None = Field(default=None, ge=0)
    python_heap_peak_mib: float | None = Field(default=None, ge=0)
    asyncio_tasks: int | None = Field(default=None, ge=0)
    pending_tasks: int | None = Field(default=None, ge=0)
    thread_count: int | None = Field(default=None, ge=0)
    handle_count: int | None = Field(default=None, ge=0)
    open_sockets: int | None = Field(default=None, ge=0)
    queue_depth: int | None = Field(default=None, ge=0)


class MemoryHealthSnapshot(BaseModel):
    """Portable diagnostic sample; unavailable platform counters stay ``None``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: MemoryHealthStatus
    measurement_time: datetime
    process_rss_mib: float | None = Field(default=None, ge=0)
    process_private_bytes_mib: float | None = Field(default=None, ge=0)
    process_uss_mib: float | None = Field(default=None, ge=0)
    python_heap_current_mib: float | None = Field(default=None, ge=0)
    python_heap_peak_mib: float | None = Field(default=None, ge=0)
    gc_objects: int | None = Field(default=None, ge=0)
    gc_generation_counts: tuple[int, int, int] | None = None
    gc_collection_counts: tuple[int, int, int] | None = None
    asyncio_tasks: int | None = Field(default=None, ge=0)
    pending_tasks: int | None = Field(default=None, ge=0)
    done_tasks: int | None = Field(default=None, ge=0)
    thread_count: int | None = Field(default=None, ge=0)
    handle_count: int | None = Field(default=None, ge=0)
    open_sockets: int | None = Field(default=None, ge=0)
    http_connection_pool_size: int | None = Field(default=None, ge=0)
    sqlite_connections: int | None = Field(default=None, ge=0)
    sqlite_cursors: int | None = Field(default=None, ge=0)
    queue_depth: int | None = Field(default=None, ge=0)
    active_books: int | None = Field(default=None, ge=0)
    active_workers: int | None = Field(default=None, ge=0)
    active_connections: int | None = Field(default=None, ge=0)
    news_cache_entries: int | None = Field(default=None, ge=0)
    universe_cache_entries: int | None = Field(default=None, ge=0)
    metrics_buffer_length: int | None = Field(default=None, ge=0)
    no_data_metrics: tuple[str, ...] = ()


class TrendStatistic(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    samples: int = Field(..., ge=0)
    slope_per_hour: float | None = None
    r_squared: float | None = Field(default=None, ge=0, le=1)
    first: float | None = None
    last: float | None = None
    p50: float | None = None
    p95: float | None = None
    maximum: float | None = None
    last_hour_change: float | None = None
    previous_hour_change: float | None = None


class MemoryWindowTrend(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    start_seconds: float = Field(..., ge=0)
    end_seconds: float = Field(..., ge=0)
    python_heap: TrendStatistic
    private_bytes: TrendStatistic
    rss: TrendStatistic
    tasks: TrendStatistic
    handles: TrendStatistic


class MemoryTrendReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: MemoryHealthStatus
    warmup_seconds: float = Field(..., ge=0)
    evaluated_span_seconds: float = Field(..., ge=0)
    python_heap: TrendStatistic
    private_bytes: TrendStatistic
    rss: TrendStatistic
    tasks: TrendStatistic
    threads: TrendStatistic
    handles: TrendStatistic
    sockets: TrendStatistic
    queue_final_depth: int | None = None
    acceptance_window_seconds: float = Field(default=7200, ge=0)
    acceptance_span_seconds: float = Field(default=0, ge=0)
    acceptance_samples: int = Field(default=0, ge=0)
    acceptance_python_heap: TrendStatistic = Field(
        default_factory=lambda: TrendStatistic(samples=0)
    )
    acceptance_private_bytes: TrendStatistic = Field(
        default_factory=lambda: TrendStatistic(samples=0)
    )
    acceptance_tasks: TrendStatistic = Field(default_factory=lambda: TrendStatistic(samples=0))
    acceptance_handles: TrendStatistic = Field(default_factory=lambda: TrendStatistic(samples=0))
    private_bytes_maximum_mib: float | None = None
    private_bytes_budget_mib: float | None = None
    plateau_detected: bool | None = None
    python_heap_required: bool = True
    windows: tuple[MemoryWindowTrend, ...] = ()
    reasons: tuple[str, ...] = ()


class ReadinessEvaluator:
    """The single fail-closed decision authority for public soak readiness."""

    @staticmethod
    def evaluate(
        criteria: list[ReadinessCriterion] | tuple[ReadinessCriterion, ...],
    ) -> ReadinessReport:
        items = tuple(criteria)
        ready = bool(items) and all(
            not item.required or item.status is CriterionStatus.PASS for item in items
        )
        return ReadinessReport(
            decision=(
                ReadinessDecision.READY_FOR_7D_SHADOW if ready else ReadinessDecision.NOT_READY
            ),
            criteria=items,
        )


def criterion(
    criterion_id: str,
    description: str,
    observed: bool | None,
    *,
    observed_value: Any = None,
    threshold: Any = None,
    evidence: tuple[str, ...] = (),
    failure_reason: str | None = None,
    required: bool = True,
) -> ReadinessCriterion:
    """Build a criterion without turning missing evidence into a false PASS."""

    status = (
        CriterionStatus.INCONCLUSIVE
        if observed is None
        else (CriterionStatus.PASS if observed else CriterionStatus.FAIL)
    )
    return ReadinessCriterion(
        criterion_id=criterion_id,
        description=description,
        required=required,
        status=status,
        observed_value=observed_value,
        threshold=threshold,
        evidence=evidence,
        failure_reason=(
            failure_reason
            if status in {CriterionStatus.FAIL, CriterionStatus.INCONCLUSIVE}
            else None
        ),
    )


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return round(ordered[index], 6)


def _regression(samples: list[MemoryTrendSample], field: str) -> TrendStatistic:
    points = [
        (sample.elapsed_seconds / 3600.0, float(value))
        for sample in samples
        if (value := getattr(sample, field)) is not None
    ]
    if not points:
        return TrendStatistic(samples=0)
    values = [value for _, value in points]
    if len(points) < 2:
        return TrendStatistic(
            samples=len(points),
            first=values[0],
            last=values[-1],
            p50=values[0],
            p95=values[0],
            maximum=values[0],
        )
    mean_x = sum(x for x, _ in points) / len(points)
    mean_y = sum(values) / len(values)
    variance_x = sum((x - mean_x) ** 2 for x, _ in points)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / variance_x if variance_x else 0.0
    fitted = [mean_y + slope * (x - mean_x) for x, _ in points]
    total = sum((value - mean_y) ** 2 for value in values)
    residual = sum((value - fit) ** 2 for value, fit in zip(values, fitted, strict=True))
    r_squared = 1.0 if total == 0 else max(0.0, min(1.0, 1.0 - residual / total))

    end = points[-1][0]
    last_hour = [value for x, value in points if x >= end - 1]
    previous_hour = [value for x, value in points if end - 2 <= x < end - 1]
    return TrendStatistic(
        samples=len(points),
        slope_per_hour=round(slope, 6),
        r_squared=round(r_squared, 6),
        first=values[0],
        last=values[-1],
        p50=round(float(median(values)), 6),
        p95=_percentile(values, 0.95),
        maximum=round(max(values), 6),
        last_hour_change=(round(last_hour[-1] - last_hour[0], 6) if len(last_hour) >= 2 else None),
        previous_hour_change=(
            round(previous_hour[-1] - previous_hour[0], 6) if len(previous_hour) >= 2 else None
        ),
    )


def evaluate_memory_trend(
    samples: list[MemoryTrendSample] | tuple[MemoryTrendSample, ...],
    *,
    warmup_seconds: float = 3600,
    minimum_samples: int = 12,
    minimum_span_seconds: float = 7200,
    python_heap_slope_limit_mib_per_hour: float = 2.0,
    private_bytes_slope_limit_mib_per_hour: float = 5.0,
    task_growth_limit_per_hour: float = 1.0,
    thread_growth_limit_per_hour: float = 1.0,
    handle_growth_limit_per_hour: float = 2.0,
    socket_growth_limit_per_hour: float = 2.0,
    queue_final_depth: int = 0,
    observed_final_queue_depth: int | None = None,
    acceptance_window_seconds: float = 7200,
    private_bytes_budget_mib: float = 512.0,
    require_python_heap: bool = True,
) -> MemoryTrendReport:
    """Evaluate the recent post-warm-up window; RSS is diagnostic only.

    A high whole-run slope caused by allocator warm-up must not hide a proven
    plateau, while a late positive slope must not be diluted by an early flat
    period. Therefore acceptance uses the final bounded window and also requires
    the final hour's private-bytes slope to remain within the same limit.
    """

    post = [sample for sample in samples if sample.elapsed_seconds >= warmup_seconds]
    span = post[-1].elapsed_seconds - post[0].elapsed_seconds if len(post) >= 2 else 0.0
    stats = {
        "python_heap": _regression(post, "python_heap_current_mib"),
        "private_bytes": _regression(post, "process_private_bytes_mib"),
        "rss": _regression(post, "process_rss_mib"),
        "tasks": _regression(post, "asyncio_tasks"),
        "threads": _regression(post, "thread_count"),
        "handles": _regression(post, "handle_count"),
        "sockets": _regression(post, "open_sockets"),
    }
    end_seconds = post[-1].elapsed_seconds if post else 0.0
    acceptance_start = max(warmup_seconds, end_seconds - acceptance_window_seconds)
    acceptance = [sample for sample in post if sample.elapsed_seconds >= acceptance_start]
    acceptance_span = (
        acceptance[-1].elapsed_seconds - acceptance[0].elapsed_seconds
        if len(acceptance) >= 2
        else 0.0
    )
    acceptance_stats = {
        "python_heap": _regression(acceptance, "python_heap_current_mib"),
        "private_bytes": _regression(acceptance, "process_private_bytes_mib"),
        "tasks": _regression(acceptance, "asyncio_tasks"),
        "threads": _regression(acceptance, "thread_count"),
        "handles": _regression(acceptance, "handle_count"),
        "sockets": _regression(acceptance, "open_sockets"),
    }
    last_hour_start = max(warmup_seconds, end_seconds - 3600)
    last_hour = [sample for sample in post if sample.elapsed_seconds >= last_hour_start]
    last_hour_private = _regression(last_hour, "process_private_bytes_mib")
    window_specs = (
        ("0-60m", 0.0, 3600.0),
        ("60-120m", 3600.0, 7200.0),
        ("120-180m", 7200.0, 10_800.0),
        ("180-240m", 10_800.0, 14_400.0),
        ("last-120m", max(0.0, end_seconds - 7200.0), end_seconds),
        ("last-60m", max(0.0, end_seconds - 3600.0), end_seconds),
    )
    windows = tuple(
        MemoryWindowTrend(
            label=label,
            start_seconds=start,
            end_seconds=end,
            python_heap=_regression(
                [item for item in samples if start <= item.elapsed_seconds <= end],
                "python_heap_current_mib",
            ),
            private_bytes=_regression(
                [item for item in samples if start <= item.elapsed_seconds <= end],
                "process_private_bytes_mib",
            ),
            rss=_regression(
                [item for item in samples if start <= item.elapsed_seconds <= end],
                "process_rss_mib",
            ),
            tasks=_regression(
                [item for item in samples if start <= item.elapsed_seconds <= end],
                "asyncio_tasks",
            ),
            handles=_regression(
                [item for item in samples if start <= item.elapsed_seconds <= end],
                "handle_count",
            ),
        )
        for label, start, end in window_specs
    )
    final_queue = (
        observed_final_queue_depth
        if observed_final_queue_depth is not None
        else (post[-1].queue_depth if post else None)
    )
    reasons: list[str] = []
    required_stats = [
        (
            "private bytes",
            acceptance_stats["private_bytes"],
            private_bytes_slope_limit_mib_per_hour,
        ),
        ("async task", acceptance_stats["tasks"], task_growth_limit_per_hour),
        ("thread", acceptance_stats["threads"], thread_growth_limit_per_hour),
        ("handle", acceptance_stats["handles"], handle_growth_limit_per_hour),
        ("socket", acceptance_stats["sockets"], socket_growth_limit_per_hour),
    ]
    if require_python_heap:
        required_stats.insert(
            0,
            (
                "Python heap",
                acceptance_stats["python_heap"],
                python_heap_slope_limit_mib_per_hour,
            ),
        )
    if len(acceptance) < minimum_samples or acceptance_span < minimum_span_seconds:
        reasons.append("acceptance window sample count or span is insufficient for a decision")
    for name, statistic, limit in required_stats:
        if statistic.slope_per_hour is None:
            reasons.append(f"{name} slope NO_DATA")
        elif statistic.slope_per_hour > limit:
            reasons.append(
                f"{name} slope {statistic.slope_per_hour:.3f}/hour is above limit {limit:.3f}"
            )
    private_values = [
        sample.process_private_bytes_mib
        for sample in samples
        if sample.process_private_bytes_mib is not None
    ]
    private_maximum = max(private_values) if private_values else None
    if private_maximum is None:
        reasons.append("private bytes maximum NO_DATA")
    elif private_maximum > private_bytes_budget_mib:
        reasons.append(
            f"private bytes maximum {private_maximum:.3f} MiB is above budget "
            f"{private_bytes_budget_mib:.3f} MiB"
        )
    last_hour_slope = last_hour_private.slope_per_hour
    if last_hour_slope is None:
        reasons.append("last 60 minutes private bytes slope NO_DATA")
    elif last_hour_slope > private_bytes_slope_limit_mib_per_hour:
        reasons.append(
            f"last 60 minutes private bytes slope {last_hour_slope:.3f}/hour is above limit "
            f"{private_bytes_slope_limit_mib_per_hour:.3f}"
        )
    if final_queue is None:
        reasons.append("final queue depth NO_DATA")
    elif final_queue != queue_final_depth:
        reasons.append(f"final queue depth {final_queue}, expected {queue_final_depth}")

    insufficient = (
        len(acceptance) < minimum_samples
        or acceptance_span < minimum_span_seconds
        or any(statistic.slope_per_hour is None for _, statistic, _ in required_stats)
        or private_maximum is None
        or last_hour_slope is None
    )
    threshold_failure = (
        any(
            statistic.slope_per_hour is not None and statistic.slope_per_hour > limit
            for _, statistic, limit in required_stats
        )
        or (private_maximum is not None and private_maximum > private_bytes_budget_mib)
        or (
            last_hour_slope is not None and last_hour_slope > private_bytes_slope_limit_mib_per_hour
        )
        or (final_queue is not None and final_queue != queue_final_depth)
    )
    if insufficient:
        status = MemoryHealthStatus.MEMORY_INCONCLUSIVE
    elif threshold_failure:
        severe = any(
            statistic.slope_per_hour is not None and statistic.slope_per_hour > limit * 2
            for _, statistic, limit in required_stats
        )
        status = MemoryHealthStatus.MEMORY_CRITICAL if severe else MemoryHealthStatus.MEMORY_WARN
    else:
        status = MemoryHealthStatus.MEMORY_OK
    return MemoryTrendReport(
        status=status,
        warmup_seconds=warmup_seconds,
        evaluated_span_seconds=round(span, 3),
        python_heap=stats["python_heap"],
        private_bytes=stats["private_bytes"],
        rss=stats["rss"],
        tasks=stats["tasks"],
        threads=stats["threads"],
        handles=stats["handles"],
        sockets=stats["sockets"],
        queue_final_depth=final_queue,
        acceptance_window_seconds=acceptance_window_seconds,
        acceptance_span_seconds=round(acceptance_span, 3),
        acceptance_samples=len(acceptance),
        acceptance_python_heap=acceptance_stats["python_heap"],
        acceptance_private_bytes=acceptance_stats["private_bytes"],
        acceptance_tasks=acceptance_stats["tasks"],
        acceptance_handles=acceptance_stats["handles"],
        private_bytes_maximum_mib=private_maximum,
        private_bytes_budget_mib=private_bytes_budget_mib,
        plateau_detected=(
            acceptance_stats["private_bytes"].slope_per_hour is not None
            and acceptance_stats["private_bytes"].slope_per_hour
            <= private_bytes_slope_limit_mib_per_hour
            and last_hour_slope is not None
            and last_hour_slope <= private_bytes_slope_limit_mib_per_hour
        ),
        python_heap_required=require_python_heap,
        windows=windows,
        reasons=tuple(reasons),
    )
