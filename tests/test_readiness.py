"""Fail-closed readiness and warm-up-aware memory acceptance tests."""

from __future__ import annotations

from argus.observability.readiness import (
    CriterionStatus,
    MemoryHealthStatus,
    MemoryTrendSample,
    ReadinessCriterion,
    ReadinessDecision,
    ReadinessEvaluator,
    ReadinessReport,
    criterion,
    evaluate_memory_trend,
)


def _sample(
    minute: int,
    *,
    heap: float,
    private: float,
    rss: float,
    tasks: int = 8,
    handles: int = 40,
    queue: int = 0,
) -> MemoryTrendSample:
    return MemoryTrendSample(
        elapsed_seconds=minute * 60,
        python_heap_current_mib=heap,
        process_private_bytes_mib=private,
        process_rss_mib=rss,
        asyncio_tasks=tasks,
        thread_count=6,
        handle_count=handles,
        open_sockets=4,
        queue_depth=queue,
    )


def test_required_missing_or_inconclusive_criterion_cannot_produce_ready() -> None:
    complete = criterion("duration", "duration complete", True)
    missing = ReadinessCriterion(
        criterion_id="memory",
        description="memory evidence",
        required=True,
        status=CriterionStatus.NOT_RUN,
    )
    inconclusive = criterion("private", "private bytes trend", None)

    for blocker in (missing, inconclusive):
        report = ReadinessEvaluator.evaluate([complete, blocker])
        assert report.decision is ReadinessDecision.NOT_READY


def test_serialized_report_is_the_independent_review_source_of_truth() -> None:
    report = ReadinessEvaluator.evaluate([criterion("all", "all required evidence", True)])
    independent = ReadinessReport.model_validate_json(report.model_dump_json())
    assert independent.decision is report.decision
    assert independent.criteria == report.criteria


def test_memory_slope_excludes_warmup_and_rss_alone_does_not_fail() -> None:
    samples: list[MemoryTrendSample] = []
    for minute in range(0, 241, 5):
        if minute < 60:
            heap = 20 + minute * 0.5
            private = 60 + minute
        else:
            hours_after_warmup = (minute - 60) / 60
            heap = 50 + hours_after_warmup
            private = 120 + hours_after_warmup * 2
        # Deliberately exceeds any sensible RSS slope; allocator RSS is diagnostic only.
        rss = 100 + minute
        samples.append(_sample(minute, heap=heap, private=private, rss=rss))

    report = evaluate_memory_trend(samples)

    assert report.status is MemoryHealthStatus.MEMORY_OK
    assert report.python_heap.slope_per_hour == 1.0
    assert report.private_bytes.slope_per_hour == 2.0
    assert report.rss.slope_per_hour == 60.0


def test_memory_is_inconclusive_without_enough_post_warmup_evidence() -> None:
    report = evaluate_memory_trend(
        [
            _sample(0, heap=10, private=30, rss=40),
            _sample(60, heap=11, private=31, rss=42),
            _sample(65, heap=11, private=31, rss=42),
        ]
    )
    assert report.status is MemoryHealthStatus.MEMORY_INCONCLUSIVE
    assert report.python_heap.slope_per_hour is not None


def test_private_or_heap_growth_fails_even_when_rss_is_flat() -> None:
    samples = [
        _sample(
            minute,
            heap=20 + (minute - 60) / 10,
            private=60 + (minute - 60) / 5,
            rss=100,
        )
        for minute in range(60, 241, 5)
    ]
    report = evaluate_memory_trend(samples)
    assert report.status is MemoryHealthStatus.MEMORY_CRITICAL
    assert report.rss.slope_per_hour == 0


def test_recent_two_hour_plateau_can_pass_after_large_warmup() -> None:
    samples = []
    for minute in range(0, 241, 5):
        private = 60 + minute * 1.5 if minute <= 120 else 240 + (minute - 120) / 30
        samples.append(_sample(minute, heap=30, private=private, rss=private + 20))

    report = evaluate_memory_trend(samples)

    assert report.status is MemoryHealthStatus.MEMORY_OK
    assert report.plateau_detected is True
    assert report.acceptance_private_bytes.slope_per_hour == 2.0
    assert [item.label for item in report.windows][-2:] == ["last-120m", "last-60m"]


def test_late_private_growth_fails_recent_acceptance_window() -> None:
    samples = [
        _sample(
            minute,
            heap=30,
            private=100 + max(0, minute - 120) / 5,
            rss=150,
        )
        for minute in range(0, 241, 5)
    ]
    report = evaluate_memory_trend(samples)
    assert report.status is MemoryHealthStatus.MEMORY_CRITICAL
    assert report.plateau_detected is False


def test_production_mode_can_explicitly_leave_python_heap_no_data() -> None:
    samples = [
        MemoryTrendSample(
            elapsed_seconds=minute * 60,
            process_private_bytes_mib=100 + minute / 60,
            process_rss_mib=120,
            asyncio_tasks=4,
            thread_count=3,
            handle_count=20,
            open_sockets=2,
            queue_depth=0,
        )
        for minute in range(0, 241, 5)
    ]
    report = evaluate_memory_trend(samples, require_python_heap=False)
    assert report.status is MemoryHealthStatus.MEMORY_OK
    assert report.python_heap_required is False
    assert report.acceptance_python_heap.slope_per_hour is None
