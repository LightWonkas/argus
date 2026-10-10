"""WatchLoop orchestrator tests — every dependency is fake (no network)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from argus.brain import CostGuard, LLMResponse, LLMRouter, TechnicalAnalyst
from argus.brain.llm import LLMClient, LLMMessage
from argus.config.models import WatchlistItem
from argus.core.contracts import OHLCVBar, OHLCVSeries
from argus.core.enums import AssetClass, Severity, Timeframe
from argus.core.errors import AdapterError
from argus.core.events import EventType
from argus.core.schemas import Provenance
from argus.ingestion.base import MarketDataAdapter
from argus.interface import (
    AlertDispatcher,
    AlertPolicy,
    CooldownTracker,
    TelegramNotifier,
    WatchLoop,
)
from argus.interface.watch_loop import ScanStatus
from argus.platform import InMemoryEventBus

NOW = datetime(2026, 5, 29, 12, 0, tzinfo=UTC)

_CANNED = (
    '{"summary":"sharp upward move","bias":"bullish","evidence_tier":"B","confidence_pct":62,'
    '"invalidation":"below 103","scenarios":[]}'
)

# Calm → shock (last bar +14%) — produces wake-worthy triggers.
_SHOCK = [100, 101, 99, 100, 101, 100, 99, 101, 100, 102, 101, 103, 102, 104, 103, 118]
_CALM = [100, 101]


class FakeLLM(LLMClient):
    def complete(
        self,
        *,
        system: str,
        messages: Sequence[LLMMessage],
        model: str,
        max_tokens: int = 8000,
        use_web_search: bool = False,
        effort: str = "high",
    ) -> LLMResponse:
        return LLMResponse(text=_CANNED, model=model, cost_usd=0.004)


class SpyLLM(FakeLLM):
    def __init__(self) -> None:
        self.models: list[str] = []
        self.web_flags: list[bool] = []

    def complete(
        self,
        *,
        system: str,
        messages: Sequence[LLMMessage],
        model: str,
        max_tokens: int = 8000,
        use_web_search: bool = False,
        effort: str = "high",
    ) -> LLMResponse:
        self.models.append(model)
        self.web_flags.append(use_web_search)
        return super().complete(
            system=system,
            messages=messages,
            model=model,
            max_tokens=max_tokens,
            use_web_search=use_web_search,
            effort=effort,
        )


class FailingAnalyst:
    def __init__(self) -> None:
        self.calls = 0

    def analyze(self, *args: object, **kwargs: object) -> object:
        self.calls += 1
        raise RuntimeError("LLM unavailable")


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


def _series(closes: list[float], *, symbol: str = "BTC/USDT", end: datetime = NOW) -> OHLCVSeries:
    n = len(closes)
    bars = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i > 0 else c
        bars.append(
            OHLCVBar(
                timestamp=end - timedelta(hours=(n - 1 - i)),
                open=o,
                high=max(o, c) + 1,
                low=min(o, c) - 1,
                close=c,
                volume=10,
            )
        )
    prov = Provenance(source="ccxt:binance", timestamp=bars[-1].timestamp, confidence=0.95)
    return OHLCVSeries(symbol=symbol, timeframe=Timeframe.H1, bars=tuple(bars), provenance=prov)


class FakeAdapter(MarketDataAdapter):
    def __init__(self, series: OHLCVSeries | None = None, *, raise_: bool = False) -> None:
        self._series = series
        self._raise = raise_

    @property
    def source(self) -> str:
        return "fake"

    def fetch_quote(self, symbol: str):
        raise NotImplementedError

    def fetch_ohlcv(self, symbol: str, timeframe: Timeframe, *, limit: int = 100) -> OHLCVSeries:
        if self._raise:
            raise AdapterError("boom")
        assert self._series is not None
        return self._series


class FakeOHLCVStore:
    """Captures saved OHLCV series (fake PIT store)."""

    def __init__(self) -> None:
        self.saved: list[OHLCVSeries] = []

    def save_series(self, series: OHLCVSeries) -> int:
        self.saved.append(series)
        return len(series.bars)


class FakeProposalStore:
    """Captures saved proposals (fake proposal journal)."""

    def __init__(self, *, raise_: bool = False) -> None:
        self.saved: list[tuple[str, str]] = []  # (symbol, source)
        self._raise = raise_

    def save_proposal(self, proposal, *, source: str) -> int:  # type: ignore[no-untyped-def]
        if self._raise:
            raise RuntimeError("disk full")
        self.saved.append((proposal.symbol, source))
        return len(self.saved)


def _loop(
    adapter: MarketDataAdapter,
    *,
    sender: FakeSender,
    bus: InMemoryEventBus,
    now: datetime = NOW,
    cooldown_s: float = 900,
    ohlcv_store: FakeOHLCVStore | None = None,
    proposal_store: FakeProposalStore | None = None,
) -> WatchLoop:
    analyst = TechnicalAnalyst(
        LLMRouter.from_single_client(
            FakeLLM(),
            model_routine="claude-sonnet-4-6",
            model_deep="claude-opus-4-8",
            guard=CostGuard(10.0),
        )
    )
    return WatchLoop(
        adapters={AssetClass.CRYPTO: adapter},
        analyst=analyst,
        notifier=TelegramNotifier(sender, [1]),
        bus=bus,
        cooldown=CooldownTracker(cooldown_s, clock=lambda: now),
        clock=lambda: now,
        ohlcv_store=ohlcv_store,
        proposal_store=proposal_store,
    )


_CRYPTO = WatchlistItem(symbol="BTC/USDT", asset_class=AssetClass.CRYPTO)


def test_analyzed_path_emits_and_notifies() -> None:
    bus = InMemoryEventBus()
    sender = FakeSender()
    loop = _loop(FakeAdapter(_series(_SHOCK)), sender=sender, bus=bus)
    result = loop.scan(_CRYPTO)

    assert result.status is ScanStatus.ANALYZED
    assert result.analysis is not None and result.analysis.confidence_pct == 62
    # The confluence proposal was produced too (next to the analysis) and is in the alert.
    assert result.proposal is not None and result.proposal.symbol == "BTC/USDT"
    assert sender.sent and sender.sent[0][0] == 1  # notification was sent
    assert "Confluence" in sender.sent[0][1]  # analysis + confluence in one message
    types = {e.type for e in bus.published}
    assert {EventType.PRICE_UPDATED, EventType.TRIGGER_FIRED, EventType.ALERT_CREATED} <= types


def test_cooldown_suppresses_second_scan() -> None:
    sender = FakeSender()
    loop = _loop(FakeAdapter(_series(_SHOCK)), sender=sender, bus=InMemoryEventBus())
    first = loop.scan(_CRYPTO)
    second = loop.scan(_CRYPTO)  # same 'now' → within the cooldown
    assert first.status is ScanStatus.ANALYZED
    assert second.status is ScanStatus.COOLDOWN
    assert len(sender.sent) == 1  # no second notification


def test_watch_scan_uses_routine_role_without_web_search() -> None:
    spy = SpyLLM()
    analyst = TechnicalAnalyst(
        LLMRouter.from_single_client(
            spy,
            model_routine="routine-model",
            model_deep="deep-model",
            guard=CostGuard(10.0),
        )
    )
    loop = _loop(FakeAdapter(_series(_SHOCK)), sender=FakeSender(), bus=InMemoryEventBus())
    loop.analyst = analyst
    assert loop.scan(_CRYPTO).status is ScanStatus.ANALYZED
    assert spy.models == ["routine-model"]
    assert spy.web_flags == [False]


def test_llm_error_keeps_deterministic_proposal_and_does_not_record_cooldown() -> None:
    sender = FakeSender()
    failing = FailingAnalyst()
    loop = _loop(FakeAdapter(_series(_SHOCK)), sender=sender, bus=InMemoryEventBus())
    loop.analyst = failing  # type: ignore[assignment]

    first = loop.scan(_CRYPTO)
    second = loop.scan(_CRYPTO)

    assert first.status is second.status is ScanStatus.ANALYSIS_ERROR
    assert first.proposal is not None
    assert failing.calls == 2  # the error was not recorded as a cooldown; retry is possible
    assert sender.sent
    assert "LLM analysis unavailable; deterministic confluence kept running." in sender.sent[0][1]


def test_tick_isolates_llm_failure_per_symbol() -> None:
    loop = _loop(FakeAdapter(_series(_SHOCK)), sender=FakeSender(), bus=InMemoryEventBus())
    loop.analyst = FailingAnalyst()  # type: ignore[assignment]
    results = loop.tick([_CRYPTO, _CRYPTO])
    assert len(results) == 2
    assert all(result.status is ScanStatus.ANALYSIS_ERROR for result in results)


def test_no_trigger_path() -> None:
    loop = _loop(FakeAdapter(_series(_CALM)), sender=FakeSender(), bus=InMemoryEventBus())
    assert loop.scan(_CRYPTO).status is ScanStatus.NO_TRIGGER


def test_adapter_error_path() -> None:
    loop = _loop(FakeAdapter(raise_=True), sender=FakeSender(), bus=InMemoryEventBus())
    result = loop.scan(_CRYPTO)
    assert result.status is ScanStatus.ERROR


def test_data_pending_when_stale() -> None:
    stale = _series(_SHOCK, end=NOW - timedelta(hours=12))  # last bar is 12 hours old
    loop = _loop(FakeAdapter(stale), sender=FakeSender(), bus=InMemoryEventBus())
    assert loop.scan(_CRYPTO).status is ScanStatus.DATA_PENDING


# ── multi-timeframe (MTF) ────────────────────────────────────────────────────


def test_tick_scans_each_configured_timeframe() -> None:
    # With timeframes configured, tick scans the symbol once per TF (symbol × TF loop).
    item = WatchlistItem(symbol="BTC/USDT", asset_class=AssetClass.CRYPTO, timeframes=["1d", "1h"])
    loop = _loop(FakeAdapter(_series(_CALM)), sender=FakeSender(), bus=InMemoryEventBus())
    results = loop.tick([item])
    assert [r.timeframe for r in results] == [Timeframe.D1, Timeframe.H1]


def test_tick_empty_timeframes_falls_back_to_loop_default() -> None:
    # Backward compatibility: empty timeframes → the loop default single TF.
    loop = _loop(FakeAdapter(_series(_CALM)), sender=FakeSender(), bus=InMemoryEventBus())
    results = loop.tick([_CRYPTO])
    assert [r.timeframe for r in results] == [Timeframe.H1]


def test_cooldown_is_timeframe_aware() -> None:
    # Different TFs of one symbol have SEPARATE cooldowns: a 1h analysis does not suppress 1d.
    sender = FakeSender()
    loop = _loop(FakeAdapter(_series(_SHOCK)), sender=sender, bus=InMemoryEventBus())
    first = loop.scan(_CRYPTO, Timeframe.H1)
    second = loop.scan(_CRYPTO, Timeframe.D1)  # different TF → not blocked by the cooldown
    third = loop.scan(_CRYPTO, Timeframe.H1)  # same TF → cooldown
    assert first.status is ScanStatus.ANALYZED
    assert second.status is ScanStatus.ANALYZED
    assert third.status is ScanStatus.COOLDOWN


def test_htf_context_included_from_series_loader() -> None:
    # With a series_loader wired in, other TF series join the analysis context (HTF context).
    htf = _series(_CALM)
    captured: list[object] = []

    class SpyAggregator:
        def aggregate(self, ctx):  # type: ignore[no-untyped-def]
            captured.append(ctx)
            from argus.decision.aggregator import default_aggregator

            return default_aggregator().aggregate(ctx)

    item = WatchlistItem(symbol="BTC/USDT", asset_class=AssetClass.CRYPTO, timeframes=["1h", "1d"])
    loop = _loop(FakeAdapter(_series(_SHOCK)), sender=FakeSender(), bus=InMemoryEventBus())
    loop.aggregator = SpyAggregator()  # type: ignore[assignment]
    loop.series_loader = lambda symbol, tf: htf if tf is Timeframe.D1 else None
    result = loop.scan(item, Timeframe.H1)
    assert result.status is ScanStatus.ANALYZED
    ctx = captured[0]
    assert Timeframe.H1 in ctx.series_by_tf  # type: ignore[attr-defined]
    assert Timeframe.D1 in ctx.series_by_tf  # type: ignore[attr-defined]


def test_proposal_recorded_on_analyzed_path() -> None:
    # The confluence proposal of a triggered scan is written to the journal (source=watch).
    store = FakeProposalStore()
    loop = _loop(
        FakeAdapter(_series(_SHOCK)),
        sender=FakeSender(),
        bus=InMemoryEventBus(),
        proposal_store=store,
    )
    assert loop.scan(_CRYPTO).status is ScanStatus.ANALYZED
    assert store.saved == [("BTC/USDT", "watch")]


def test_proposal_store_error_does_not_break_scan() -> None:
    # A failing proposal journal does not break the scan/notification (fail-safe).
    sender = FakeSender()
    loop = _loop(
        FakeAdapter(_series(_SHOCK)),
        sender=sender,
        bus=InMemoryEventBus(),
        proposal_store=FakeProposalStore(raise_=True),
    )
    result = loop.scan(_CRYPTO)
    assert result.status is ScanStatus.ANALYZED
    assert sender.sent  # the notification was still sent


def test_ohlcv_persisted_on_quality_pass() -> None:
    # A series that passes quality (even trigger-less CALM) is stored → the panel sees
    # fresh data.
    store = FakeOHLCVStore()
    loop = _loop(
        FakeAdapter(_series(_CALM)), sender=FakeSender(), bus=InMemoryEventBus(), ohlcv_store=store
    )
    result = loop.scan(_CRYPTO)
    assert result.status is ScanStatus.NO_TRIGGER  # no trigger
    assert len(store.saved) == 1  # but OHLCV was still persisted
    assert store.saved[0].symbol == "BTC/USDT"


def test_ohlcv_not_persisted_when_quality_fails() -> None:
    store = FakeOHLCVStore()
    stale = _series(_SHOCK, end=NOW - timedelta(hours=12))  # quality fails (stale)
    loop = _loop(FakeAdapter(stale), sender=FakeSender(), bus=InMemoryEventBus(), ohlcv_store=store)
    assert loop.scan(_CRYPTO).status is ScanStatus.DATA_PENDING
    assert store.saved == []  # broken/stale data is not persisted


def test_ohlcv_store_error_does_not_break_scan() -> None:
    class BrokenStore(FakeOHLCVStore):
        def save_series(self, series: OHLCVSeries) -> int:
            raise RuntimeError("disk full")

    loop = _loop(
        FakeAdapter(_series(_SHOCK)),
        sender=FakeSender(),
        bus=InMemoryEventBus(),
        ohlcv_store=BrokenStore(),
    )
    # A storage error does not break the scan flow (fail-safe) → the analysis completes.
    assert loop.scan(_CRYPTO).status is ScanStatus.ANALYZED


def test_skipped_when_no_adapter() -> None:
    loop = _loop(FakeAdapter(_series(_SHOCK)), sender=FakeSender(), bus=InMemoryEventBus())
    bist_item = WatchlistItem(symbol="THYAO", asset_class=AssetClass.BIST)
    assert loop.scan(bist_item).status is ScanStatus.SKIPPED


def test_closed_market_skips_fetch() -> None:
    # BIST adapter is registered but it is 03:00 UTC → session closed → no fetch
    sender = FakeSender()
    analyst = TechnicalAnalyst(
        LLMRouter.from_single_client(
            FakeLLM(),
            model_routine="claude-sonnet-4-6",
            model_deep="claude-opus-4-8",
            guard=CostGuard(10.0),
        )
    )
    closed_now = datetime(2026, 5, 29, 3, 0, tzinfo=UTC)
    loop = WatchLoop(
        adapters={AssetClass.BIST: FakeAdapter(raise_=True)},  # raises if called
        analyst=analyst,
        notifier=TelegramNotifier(sender, [1]),
        bus=InMemoryEventBus(),
        clock=lambda: closed_now,
    )
    result = loop.scan(WatchlistItem(symbol="THYAO", asset_class=AssetClass.BIST))
    assert result.status is ScanStatus.CLOSED  # the adapter was never called (not ERROR)


def test_tick_scans_all() -> None:
    loop = _loop(FakeAdapter(_series(_SHOCK)), sender=FakeSender(), bus=InMemoryEventBus())
    results = loop.tick([_CRYPTO, _CRYPTO])
    assert len(results) == 2


def test_dispatcher_routes_to_digest_not_instant() -> None:
    # Dispatcher + policy (only CRITICAL is instant) → a HIGH shock goes to the DIGEST
    sender = FakeSender()
    analyst = TechnicalAnalyst(
        LLMRouter.from_single_client(
            FakeLLM(),
            model_routine="claude-sonnet-4-6",
            model_deep="claude-opus-4-8",
            guard=CostGuard(10.0),
        )
    )
    dispatcher = AlertDispatcher(
        TelegramNotifier(sender, [1]),
        policy=AlertPolicy(min_instant_severity=Severity.CRITICAL),
        clock=lambda: NOW,
    )
    loop = WatchLoop(
        adapters={AssetClass.CRYPTO: FakeAdapter(_series(_SHOCK))},
        analyst=analyst,
        notifier=TelegramNotifier(sender, [1]),
        bus=InMemoryEventBus(),
        cooldown=CooldownTracker(900, clock=lambda: NOW),
        clock=lambda: NOW,
        dispatcher=dispatcher,
    )
    result = loop.scan(_CRYPTO)
    assert result.status is ScanStatus.ANALYZED
    assert sender.sent == []  # no instant send (deferred to the digest)
    assert len(dispatcher.pending_digest) == 1


def test_circuit_breaker_degraded_mode() -> None:
    # circuit_threshold=1: the first error disables the source → the next scan is DEGRADED
    sender = FakeSender()
    analyst = TechnicalAnalyst(
        LLMRouter.from_single_client(
            FakeLLM(),
            model_routine="claude-sonnet-4-6",
            model_deep="claude-opus-4-8",
            guard=CostGuard(10.0),
        )
    )
    loop = WatchLoop(
        adapters={AssetClass.CRYPTO: FakeAdapter(raise_=True)},
        analyst=analyst,
        notifier=TelegramNotifier(sender, [1]),
        bus=InMemoryEventBus(),
        clock=lambda: NOW,
        circuit_threshold=1,
    )
    first = loop.scan(_CRYPTO)
    assert first.status is ScanStatus.ERROR  # error → opened the circuit
    assert len(sender.sent) == 1  # the "flying blind" alert was sent once

    second = loop.scan(_CRYPTO)
    assert second.status is ScanStatus.DEGRADED  # circuit open → skipped
    assert len(sender.sent) == 1  # no repeated alert


def test_fetch_retry_recovers_transient_error() -> None:
    class FlakyAdapter(MarketDataAdapter):
        def __init__(self, series: OHLCVSeries) -> None:
            self._series = series
            self.calls = 0

        @property
        def source(self) -> str:
            return "flaky"

        def fetch_quote(self, symbol: str):
            raise NotImplementedError

        def fetch_ohlcv(
            self, symbol: str, timeframe: Timeframe, *, limit: int = 100
        ) -> OHLCVSeries:
            self.calls += 1
            if self.calls == 1:
                raise AdapterError("transient error")
            return self._series

    sender = FakeSender()
    analyst = TechnicalAnalyst(
        LLMRouter.from_single_client(
            FakeLLM(),
            model_routine="claude-sonnet-4-6",
            model_deep="claude-opus-4-8",
            guard=CostGuard(10.0),
        )
    )
    loop = WatchLoop(
        adapters={AssetClass.CRYPTO: FlakyAdapter(_series(_SHOCK))},
        analyst=analyst,
        notifier=TelegramNotifier(sender, [1]),
        bus=InMemoryEventBus(),
        cooldown=CooldownTracker(900, clock=lambda: NOW),
        clock=lambda: NOW,
        fetch_retries=2,  # 1st attempt fails, 2nd succeeds
    )
    assert loop.scan(_CRYPTO).status is ScanStatus.ANALYZED


def test_journal_records_analysis() -> None:
    from argus.storage import SqliteStore

    store = SqliteStore()
    sender = FakeSender()
    analyst = TechnicalAnalyst(
        LLMRouter.from_single_client(
            FakeLLM(),
            model_routine="claude-sonnet-4-6",
            model_deep="claude-opus-4-8",
            guard=CostGuard(10.0),
        )
    )
    loop = WatchLoop(
        adapters={AssetClass.CRYPTO: FakeAdapter(_series(_SHOCK))},
        analyst=analyst,
        notifier=TelegramNotifier(sender, [1]),
        bus=InMemoryEventBus(),
        cooldown=CooldownTracker(900, clock=lambda: NOW),
        clock=lambda: NOW,
        journal=store,
    )
    assert loop.scan(_CRYPTO).status is ScanStatus.ANALYZED
    recent = store.recent_analyses()
    assert len(recent) == 1
    assert recent[0].analysis.symbol == "BTC/USDT"
