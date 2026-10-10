"""Dashboard analysis/hit-rate/alert endpoint tests (api.app journal) — offline, fake reader."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from argus.api.app import create_app
from argus.core.contracts import AnalysisOutput, Scenario
from argus.core.enums import EvidenceTier, Timeframe
from argus.storage.base import AlertRecord, HitRate, NewsRecord, OutcomeBucket, StoredAnalysis

BASE = datetime(2026, 5, 30, 9, 0, tzinfo=UTC)


def _analysis(symbol: str = "BTC/USDT", bias: str = "bullish") -> AnalysisOutput:
    return AnalysisOutput(
        symbol=symbol,
        timeframe=Timeframe.H1,
        summary=f"{symbol} short-term {bias} bias.",
        bias=bias,
        evidence_tier=EvidenceTier.A,
        confidence_pct=72,
        invalidation="H1 close below 60000",
        scenarios=(Scenario(name="Base", probability_pct=60, detail="Uptrend continues"),),
        model="claude-sonnet-4-6",
    )


class FakeJournal:
    """Fake JournalReader — injected via DI (offline)."""

    def __init__(self) -> None:
        self._stored = [
            StoredAnalysis(id=1, created_at=BASE, analysis=_analysis("BTC/USDT", "bullish")),
            StoredAnalysis(id=2, created_at=BASE, analysis=_analysis("ETH/USDT", "bearish")),
        ]
        self._alerts = [
            AlertRecord(
                analysis_id=2,
                created_at=BASE,
                symbol="ETH/USDT",
                severity="high",
                kind="rsi_overbought",
                message="RSI 78 overbought",
            ),
            AlertRecord(
                analysis_id=1,
                created_at=BASE,
                symbol="BTC/USDT",
                severity="medium",
                kind="price_move",
                message="5% move",
            ),
        ]
        self._news = [
            NewsRecord(
                source="kap",
                ext_id="d1",
                kind="disclosure",
                title="ACME published financial results",
                url="https://example.com/x",
                symbol="ACME",
                published_at=BASE,
            ),
            NewsRecord(
                source="cryptopanic",
                ext_id="n1",
                kind="news",
                title="BTC news",
                url=None,
                symbol="BTCUSDT",
                published_at=BASE,
            ),
        ]

    def recent_analyses(
        self, *, symbol: str | None = None, limit: int = 20
    ) -> list[StoredAnalysis]:
        rows = (
            self._stored
            if symbol is None
            else [s for s in self._stored if s.analysis.symbol == symbol]
        )
        return rows[:limit]

    def recent_alerts(self, *, limit: int = 50) -> list[AlertRecord]:
        return self._alerts[:limit]

    def recent_news(self, *, source: str | None = None, limit: int = 50) -> list[NewsRecord]:
        rows = self._news if source is None else [n for n in self._news if n.source == source]
        return rows[:limit]

    def hit_rate(self, *, symbol: str | None = None) -> HitRate:
        return HitRate(correct=3, total=4)

    def hit_rate_v2(self) -> list[OutcomeBucket]:
        # v2 buckets: system 3/4 (close) + random baseline 1/4 → delta_pts.close = +50.0
        return [
            OutcomeBucket("analysis", "close", "system", correct=3, total=4),
            OutcomeBucket("analysis", "touch", "system", correct=4, total=4),
            OutcomeBucket("analysis", "close", "random", correct=1, total=4),
            OutcomeBucket("analysis", "touch", "random", correct=2, total=4),
            OutcomeBucket("analysis", "close", "bullish", correct=2, total=4),
            OutcomeBucket("proposal", "close", "system", correct=1, total=2),
        ]


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(journal_provider=FakeJournal)  # no auth → dev mode
    with TestClient(app) as c:
        yield c


@pytest.fixture
def no_journal_client() -> Iterator[TestClient]:
    app = create_app(journal_provider=lambda: None)  # journal not connected
    with TestClient(app) as c:
        yield c


# --- /api/analyses ---


def test_analyses_list(client: TestClient) -> None:
    r = client.get("/api/analyses")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    assert body[0]["symbol"] == "BTC/USDT"
    assert body[0]["bias"] == "bullish"
    assert body[0]["evidence_tier"] == "A"
    assert "summary" in body[0]


def test_analyses_filter_by_symbol(client: TestClient) -> None:
    r = client.get("/api/analyses", params={"symbol": "ETH/USDT"})
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["symbol"] == "ETH/USDT"


def test_analysis_detail_full(client: TestClient) -> None:
    r = client.get("/api/analyses/1")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == 1
    assert body["analysis"]["symbol"] == "BTC/USDT"
    assert body["analysis"]["scenarios"][0]["name"] == "Base"
    assert "not financial advice" in body["analysis"]["disclaimer"].lower()


def test_analysis_detail_not_found(client: TestClient) -> None:
    assert client.get("/api/analyses/999").status_code == 404


# --- /api/hitrate ---


def test_hitrate(client: TestClient) -> None:
    r = client.get("/api/hitrate")
    assert r.status_code == 200
    body = r.json()
    # v1 backward-compatible fields
    assert body["correct"] == 3
    assert body["total"] == 4
    assert body["rate_pct"] == 75.0
    # v2: series + baselines + delta + low-sample flag
    llm = body["series"]["llm"]
    assert llm["close"] == {"hit": 3, "n": 4, "pct": 75.0}
    assert llm["touch"]["pct"] == 100.0
    assert llm["baselines"]["random"]["close"]["pct"] == 25.0
    assert llm["delta_pts"]["close"] == 50.0  # system 75 − random 25
    assert llm["delta_pts"]["touch"] == 50.0  # 100 − 50
    assert llm["low_n"] is True  # n=4 < min_n
    assert body["low_n"] is True
    prop = body["series"]["proposal"]
    assert prop["close"]["n"] == 2
    assert prop["delta_pts"]["close"] is None  # no random baseline → no delta can be built (honest)


# --- /api/alerts ---


def test_alerts_list(client: TestClient) -> None:
    r = client.get("/api/alerts")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    assert body[0]["kind"] == "rsi_overbought"
    assert body[0]["severity"] == "high"


def test_alerts_filter_by_severity(client: TestClient) -> None:
    r = client.get("/api/alerts", params={"severity": "medium"})
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["severity"] == "medium"


# --- /api/news ---


def test_news_list(client: TestClient) -> None:
    r = client.get("/api/news")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    kinds = {n["kind"] for n in body}
    assert kinds == {"disclosure", "news"}
    assert body[0]["title"] == "ACME published financial results"


def test_news_filter_by_source(client: TestClient) -> None:
    r = client.get("/api/news", params={"source": "kap"})
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["source"] == "kap"


def test_news_503_when_no_journal(no_journal_client: TestClient) -> None:
    assert no_journal_client.get("/api/news").status_code == 503


# --- Journal not connected → 503 (honest DATA PENDING) ---


def test_analyses_503_when_no_journal(no_journal_client: TestClient) -> None:
    r = no_journal_client.get("/api/analyses")
    assert r.status_code == 503
    assert "DATA PENDING" in r.json()["detail"]


def test_hitrate_503_when_no_journal(no_journal_client: TestClient) -> None:
    assert no_journal_client.get("/api/hitrate").status_code == 503


# --- Auth: journal endpoints are protected too ---


def test_journal_endpoints_require_auth() -> None:
    app = create_app(journal_provider=FakeJournal, users={"ada": "scrypt$x"})
    client = TestClient(app)
    for path in ("/api/analyses", "/api/hitrate", "/api/alerts", "/api/analyses/1", "/api/news"):
        assert client.get(path).status_code == 401, path
