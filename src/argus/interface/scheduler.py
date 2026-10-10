"""Watch scheduler (pull/heartbeat) — runs the WatchLoop at per-tier intervals.

T0 holdings are scanned frequently, T1 watchlist moderately, T2 broad lightly.
``tier_jobs`` and ``run_once`` are pure/testable; ``start()`` is the blocking 24/7 daemon
built on APScheduler (lazy import). Push events are handled by the Redis consumer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from argus.config.models import RuntimeConfig, WatchlistItem, WatchTier
from argus.interface.dispatcher import AlertDispatcher
from argus.interface.notifier import DeliveryResult
from argus.interface.watch_loop import ScanResult
from argus.observability.logging import get_logger

_log = get_logger("argus.interface.scheduler")

# Scan tiers — ordered frequent → light.
_TIERS: tuple[WatchTier, ...] = ("holdings", "watchlist", "broad")


class SupportsTick(Protocol):
    """Any object that scans a group of items (WatchLoop satisfies this)."""

    def tick(self, items: list[WatchlistItem]) -> list[ScanResult]: ...


class SupportsRunOnce(Protocol):
    """Periodic side job, independent of the main watch, with isolated failures."""

    def run_once(self) -> object: ...


class SupportsClose(Protocol):
    def close(self) -> None: ...


@dataclass(frozen=True)
class TierJob:
    """Scheduled scan job for one tier."""

    tier: WatchTier
    interval_s: int
    items: list[WatchlistItem]


def tier_jobs(config: RuntimeConfig) -> list[TierJob]:
    """Split the configured watchlist into tier jobs (empty tiers are skipped).

    Scan intervals are read from ``config.loop`` (config = data).
    """
    jobs: list[TierJob] = []
    for tier in _TIERS:
        items = config.by_tier(tier)
        if items:
            jobs.append(TierJob(tier=tier, interval_s=config.loop.interval_for(tier), items=items))
    return jobs


class WatchScheduler:
    """Runs the WatchLoop periodically per tier."""

    def __init__(
        self,
        loop: SupportsTick,
        config: RuntimeConfig,
        *,
        dispatcher: AlertDispatcher | None = None,
        digest_interval_s: int | None = None,
        crypto_cycle: SupportsRunOnce | None = None,
        crypto_interval_s: int | None = None,
        news_cycle: SupportsRunOnce | None = None,
        news_interval_s: int | None = None,
    ) -> None:
        self._loop = loop
        self._jobs = tier_jobs(config)
        self._dispatcher = dispatcher
        self._digest_interval_s = digest_interval_s
        self._crypto_cycle = crypto_cycle
        self._crypto_interval_s = crypto_interval_s
        self._news_cycle = news_cycle
        self._news_interval_s = news_interval_s

    @property
    def jobs(self) -> list[TierJob]:
        return self._jobs

    def run_once(self) -> list[ScanResult]:
        """Scan every tier once (manual/test run)."""
        results: list[ScanResult] = []
        for job in self._jobs:
            results.extend(self._loop.tick(job.items))
        if self._crypto_cycle is not None:
            try:
                self._crypto_cycle.run_once()
            except Exception as exc:
                _log.error("crypto-sidecar-error", error=str(exc))
        if self._news_cycle is not None:
            try:
                self._news_cycle.run_once()
            except Exception as exc:
                _log.error("news-sidecar-error", error=type(exc).__name__)
        return results

    def run_digest(self) -> list[DeliveryResult]:
        """Publish the accumulated digest (daily briefing); empty without a dispatcher."""
        if self._dispatcher is None:
            return []
        return self._dispatcher.flush_digest()

    def start(self) -> None:
        """Start the blocking 24/7 daemon with APScheduler (live)."""
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.interval import IntervalTrigger

        scheduler = BlockingScheduler(timezone="UTC")
        for job in self._jobs:
            scheduler.add_job(
                self._loop.tick,
                IntervalTrigger(seconds=job.interval_s),
                args=[job.items],
                id=f"tier-{job.tier}",
                max_instances=1,
                coalesce=True,
            )
        if self._dispatcher is not None and self._digest_interval_s:
            scheduler.add_job(
                self.run_digest,
                IntervalTrigger(seconds=self._digest_interval_s),
                id="digest-briefing",
                max_instances=1,
                coalesce=True,
            )
        if self._crypto_cycle is not None and self._crypto_interval_s:
            scheduler.add_job(
                self._crypto_cycle.run_once,
                IntervalTrigger(seconds=self._crypto_interval_s),
                id="crypto-rest-reconcile",
                max_instances=1,
                coalesce=True,
            )
        if self._news_cycle is not None and self._news_interval_s:
            scheduler.add_job(
                self._news_cycle.run_once,
                IntervalTrigger(seconds=self._news_interval_s),
                id="news-intelligence-refresh",
                max_instances=1,
                coalesce=True,
            )
        _log.info(
            "watch-scheduler-started",
            tiers=[(j.tier, j.interval_s, len(j.items)) for j in self._jobs],
            digest_s=self._digest_interval_s,
        )
        try:
            scheduler.start()
        finally:
            close = getattr(self._crypto_cycle, "close", None)
            if callable(close):
                close()
            news_close = getattr(self._news_cycle, "close", None)
            if callable(news_close):
                news_close()
