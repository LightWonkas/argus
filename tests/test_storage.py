"""SqliteStore tests (:memory: — no extra dependencies, no network)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from argus.core.contracts import (
    AnalysisOutput,
    Disclosure,
    NewsItem,
    OHLCVBar,
    OHLCVSeries,
    Scenario,
)
from argus.core.enums import EvidenceTier, Severity, Timeframe
from argus.core.schemas import Provenance
from argus.platform.triggers import TriggerKind, TriggerSignal
from argus.storage import SqliteStore

BASE = datetime(2026, 5, 29, 12, 0, tzinfo=UTC)


def _series(n: int = 1, *, symbol: str = "BTC/USDT") -> OHLCVSeries:
    bars = tuple(
        OHLCVBar(
            timestamp=BASE + timedelta(hours=i),
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("90"),
            close=Decimal("100") + i,
            volume=Decimal("1.5"),
        )
        for i in range(n)
    )
    prov = Provenance(source="ccxt:binance", timestamp=bars[-1].timestamp, confidence=0.95)
    return OHLCVSeries(symbol=symbol, timeframe=Timeframe.H1, bars=bars, provenance=prov)


def _analysis(symbol: str = "BTC/USDT") -> AnalysisOutput:
    return AnalysisOutput(
        symbol=symbol,
        timeframe=Timeframe.H1,
        summary="summary",
        bias="bullish",
        evidence_tier=EvidenceTier.B,
        confidence_pct=62,
        invalidation="below 100",
        scenarios=(Scenario(name="Base", probability_pct=60, detail="x"),),
        model="claude-opus-4-8",
    )


def test_ohlcv_roundtrip_preserves_decimal() -> None:
    store = SqliteStore()
    bar = OHLCVBar(
        timestamp=BASE,
        open=Decimal("100.5"),
        high=Decimal("60000"),
        low=Decimal("90"),
        close=Decimal("50000.12345678"),
        volume=Decimal("1.23456789"),
    )
    series = OHLCVSeries(
        symbol="BTC/USDT",
        timeframe=Timeframe.H1,
        bars=(bar,),
        provenance=Provenance(source="ccxt:binance", timestamp=BASE, confidence=0.9),
    )
    assert store.save_series(series) == 1
    loaded = store.load_bars("BTC/USDT", Timeframe.H1)
    assert loaded[0].close == Decimal("50000.12345678")  # precision preserved
    assert loaded[0].volume == Decimal("1.23456789")


def test_ohlcv_upsert_idempotent() -> None:
    store = SqliteStore()
    series = _series(3)
    store.save_series(series)
    store.save_series(series)  # same bars again
    assert len(store.load_bars("BTC/USDT", Timeframe.H1)) == 3  # not duplicated


def test_load_bars_limit_and_ascending() -> None:
    store = SqliteStore()
    store.save_series(_series(5))
    bars = store.load_bars("BTC/USDT", Timeframe.H1, limit=3)
    assert len(bars) == 3
    ts = [b.timestamp for b in bars]
    assert ts == sorted(ts)  # ascending order


def test_load_series_reconstructs_provenance() -> None:
    store = SqliteStore()
    store.save_series(_series(2))
    series = store.load_series("BTC/USDT", Timeframe.H1)
    assert series.provenance.source == "ccxt:binance"
    assert len(series.bars) == 2


def test_record_and_recent_analyses_roundtrip() -> None:
    store = SqliteStore()
    sig = TriggerSignal(
        symbol="BTC/USDT",
        timeframe=Timeframe.H1,
        kind=TriggerKind.RSI_OVERBOUGHT,
        severity=Severity.MEDIUM,
        message="RSI 77",
    )
    aid = store.record_analysis(_analysis(), signals=[sig])
    assert aid > 0
    recent = store.recent_analyses()
    assert len(recent) == 1
    assert recent[0].analysis.symbol == "BTC/USDT"
    assert recent[0].analysis.evidence_tier is EvidenceTier.B
    assert len(recent[0].analysis.scenarios) == 1  # JSON lossless


def test_recent_analyses_filter_by_symbol() -> None:
    store = SqliteStore()
    store.record_analysis(_analysis("BTC/USDT"))
    store.record_analysis(_analysis("ETH/USDT"))
    btc = store.recent_analyses(symbol="BTC/USDT")
    assert len(btc) == 1 and btc[0].analysis.symbol == "BTC/USDT"


def test_recent_alerts_flattens_signals() -> None:
    store = SqliteStore()
    sig1 = TriggerSignal(
        symbol="BTC/USDT",
        timeframe=Timeframe.H1,
        kind=TriggerKind.RSI_OVERBOUGHT,
        severity=Severity.HIGH,
        message="RSI 78",
    )
    sig2 = TriggerSignal(
        symbol="BTC/USDT",
        timeframe=Timeframe.H1,
        kind=TriggerKind.PRICE_MOVE,
        severity=Severity.MEDIUM,
        message="+5.00% price move",
    )
    store.record_analysis(_analysis("BTC/USDT"), signals=[sig1, sig2])
    store.record_analysis(_analysis("ETH/USDT"))  # analysis without triggers → produces no alert

    alerts = store.recent_alerts()
    assert len(alerts) == 2  # two triggers of one analysis were flattened
    kinds = {a.kind for a in alerts}
    assert kinds == {"rsi_overbought", "price_move"}
    assert all(a.symbol == "BTC/USDT" for a in alerts)


def test_recent_alerts_empty_when_no_signals() -> None:
    store = SqliteStore()
    store.record_analysis(_analysis("BTC/USDT"))  # signals=()
    assert store.recent_alerts() == []


# ── News / KAP disclosure archive ───────────────────────────────────────────


def _news(nid: str, *, symbols: tuple[str, ...] = ("BTCUSDT",), when: datetime = BASE) -> NewsItem:
    return NewsItem(
        id=nid,
        title=f"News {nid}",
        published_at=when,
        symbols=symbols,
        source_name="CryptoPanic",
        provenance=Provenance(source="cryptopanic", timestamp=when, confidence=0.8),
    )


def _disclosure(did: str, *, when: datetime = BASE) -> Disclosure:
    return Disclosure(
        disclosure_id=did,
        symbol="THYAO",
        title=f"KAP {did}",
        published_at=when,
        provenance=Provenance(source="kap", timestamp=when, confidence=0.95),
    )


def test_save_and_recent_news_mixed() -> None:
    store = SqliteStore()
    added = store.save_news([_news("n1"), _disclosure("d1")])
    assert added == 2
    recent = store.recent_news()
    assert len(recent) == 2
    kinds = {r.kind for r in recent}
    assert kinds == {"news", "disclosure"}
    # A NewsItem stores its first symbol; a Disclosure stores its own symbol.
    by_kind = {r.kind: r for r in recent}
    assert by_kind["news"].symbol == "BTCUSDT"
    assert by_kind["disclosure"].symbol == "THYAO"


def test_save_news_dedup_idempotent() -> None:
    store = SqliteStore()
    store.save_news([_news("n1")])
    added = store.save_news([_news("n1"), _news("n2")])  # n1 again → skipped
    assert added == 1  # only n2 is new
    assert len(store.recent_news()) == 2


def test_recent_news_ordered_newest_first() -> None:
    store = SqliteStore()
    store.save_news([_news("old", when=BASE), _news("new", when=BASE + timedelta(hours=2))])
    recent = store.recent_news()
    assert recent[0].ext_id == "new"  # newest first


def test_recent_news_filter_by_source() -> None:
    store = SqliteStore()
    store.save_news([_news("n1"), _disclosure("d1")])
    kap = store.recent_news(source="kap")
    assert len(kap) == 1 and kap[0].kind == "disclosure"


# ── Copilot chat history ────────────────────────────────────────────────────


def test_chat_save_and_recent_ordered() -> None:
    store = SqliteStore()
    store.save_chat("user", "hello")
    store.save_chat("assistant", "hi, how can I help?")
    store.save_chat("user", "BTC?")
    turns = store.recent_chat()
    assert len(turns) == 3
    # Oldest→newest order (natural UI flow).
    assert turns[0].role == "user" and turns[0].content == "hello"
    assert turns[-1].content == "BTC?"


def test_chat_recent_limit() -> None:
    store = SqliteStore()
    for i in range(5):
        store.save_chat("user", f"m{i}")
    turns = store.recent_chat(limit=2)
    assert len(turns) == 2
    assert turns[-1].content == "m4"  # newest 2, oldest→newest


def test_hit_rate() -> None:
    store = SqliteStore()
    assert store.hit_rate().rate == 0.0  # no outcomes
    a1 = store.record_analysis(_analysis())
    a2 = store.record_analysis(_analysis())
    a3 = store.record_analysis(_analysis())
    store.record_outcome(a1, correct=True)
    store.record_outcome(a2, correct=True)
    store.record_outcome(a3, correct=False)
    hr = store.hit_rate()
    assert hr.total == 3
    assert hr.correct == 2
    assert hr.rate == 2 / 3


def test_hit_rate_by_symbol() -> None:
    store = SqliteStore()
    a_btc = store.record_analysis(_analysis("BTC/USDT"))
    a_eth = store.record_analysis(_analysis("ETH/USDT"))
    store.record_outcome(a_btc, correct=True)
    store.record_outcome(a_eth, correct=False)
    assert store.hit_rate(symbol="BTC/USDT").rate == 1.0
    assert store.hit_rate(symbol="ETH/USDT").rate == 0.0


def test_cooldown_persistence_roundtrip() -> None:
    store = SqliteStore()
    assert store.get_cooldown("BTC/USDT") is None
    store.set_cooldown("BTC/USDT", BASE)
    assert store.get_cooldown("BTC/USDT") == BASE
    later = BASE + timedelta(hours=1)
    store.set_cooldown("BTC/USDT", later)  # upsert
    assert store.get_cooldown("BTC/USDT") == later
