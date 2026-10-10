"""Watch scheduler tests — pure planning + run_once (no APScheduler required)."""

from __future__ import annotations

from argus.config.models import RuntimeConfig, WatchlistItem
from argus.core.enums import AssetClass
from argus.interface import ScanResult, ScanStatus, TierJob, WatchScheduler, tier_jobs


def _config() -> RuntimeConfig:
    return RuntimeConfig(
        watchlist=[
            WatchlistItem(symbol="BTC/USDT", asset_class=AssetClass.CRYPTO, tier="holdings"),
            WatchlistItem(symbol="THYAO", asset_class=AssetClass.BIST, tier="watchlist"),
            WatchlistItem(symbol="XU100", asset_class=AssetClass.BIST, tier="broad"),
            WatchlistItem(symbol="GARAN", asset_class=AssetClass.BIST, tier="watchlist"),
        ]
    )


class FakeLoop:
    """SupportsTick — records tick calls."""

    def __init__(self) -> None:
        self.calls: list[list[WatchlistItem]] = []

    def tick(self, items: list[WatchlistItem]) -> list[ScanResult]:
        self.calls.append(list(items))
        return [ScanResult(i.symbol, ScanStatus.NO_TRIGGER) for i in items]


def test_tier_jobs_grouping_and_intervals() -> None:
    jobs = tier_jobs(_config())
    by_tier = {j.tier: j for j in jobs}
    assert set(by_tier) == {"holdings", "watchlist", "broad"}
    assert by_tier["holdings"].interval_s == 300
    assert by_tier["watchlist"].interval_s == 900
    assert by_tier["broad"].interval_s == 3600
    assert len(by_tier["watchlist"].items) == 2  # THYAO + GARAN


def test_tier_jobs_order_frequent_first() -> None:
    tiers = [j.tier for j in tier_jobs(_config())]
    assert tiers == ["holdings", "watchlist", "broad"]


def test_tier_jobs_skips_empty_tiers() -> None:
    cfg = RuntimeConfig(
        watchlist=[WatchlistItem(symbol="BTC/USDT", asset_class=AssetClass.CRYPTO, tier="holdings")]
    )
    jobs = tier_jobs(cfg)
    assert len(jobs) == 1
    assert jobs[0].tier == "holdings"


def test_tier_jobs_empty_config() -> None:
    assert tier_jobs(RuntimeConfig()) == []


def test_run_once_ticks_every_tier() -> None:
    loop = FakeLoop()
    scheduler = WatchScheduler(loop, _config())
    results = scheduler.run_once()

    # 3 tick calls (holdings/watchlist/broad), 4 items in total
    assert len(loop.calls) == 3
    assert sum(len(c) for c in loop.calls) == 4
    assert len(results) == 4


def test_run_once_isolates_crypto_sidecar_failure() -> None:
    class BrokenCryptoCycle:
        def run_once(self) -> object:
            raise RuntimeError("derivatives unavailable")

    loop = FakeLoop()
    scheduler = WatchScheduler(loop, _config(), crypto_cycle=BrokenCryptoCycle())
    results = scheduler.run_once()
    assert len(results) == 4
    assert len(loop.calls) == 3


def test_jobs_property() -> None:
    scheduler = WatchScheduler(FakeLoop(), _config())
    assert all(isinstance(j, TierJob) for j in scheduler.jobs)
    assert len(scheduler.jobs) == 3


def test_tier_jobs_uses_config_intervals() -> None:
    from argus.config.models import LoopConfig

    cfg = RuntimeConfig(
        watchlist=[
            WatchlistItem(symbol="BTC/USDT", asset_class=AssetClass.CRYPTO, tier="holdings")
        ],
        loop=LoopConfig(holdings_interval_s=60),
    )
    jobs = tier_jobs(cfg)
    assert jobs[0].interval_s == 60  # from config (not hardcoded)


def test_run_digest_without_dispatcher_is_empty() -> None:
    assert WatchScheduler(FakeLoop(), _config()).run_digest() == []


def test_run_digest_flushes_via_dispatcher() -> None:
    from argus.core.contracts import AnalysisOutput
    from argus.core.enums import EvidenceTier, Severity, Timeframe
    from argus.interface import AlertDispatcher, TelegramNotifier

    class FakeSender:
        def __init__(self) -> None:
            self.sent: list[tuple[int, str]] = []

        def send_message(self, chat_id: int, text: str) -> None:
            self.sent.append((chat_id, text))

    sender = FakeSender()
    dispatcher = AlertDispatcher(TelegramNotifier(sender, [1]))
    analysis = AnalysisOutput(
        symbol="BTC/USDT",
        timeframe=Timeframe.H1,
        summary="s",
        evidence_tier=EvidenceTier.B,
        confidence_pct=50,
        invalidation="x",
        model="m",
    )
    dispatcher.dispatch_analysis(analysis, Severity.MEDIUM)  # default policy → digest

    scheduler = WatchScheduler(
        FakeLoop(), _config(), dispatcher=dispatcher, digest_interval_s=86_400
    )
    results = scheduler.run_digest()
    assert len(results) == 1
    assert "Daily briefing" in sender.sent[0][1]
