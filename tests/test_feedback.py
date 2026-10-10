"""User-value metric tests — store + callback helpers + API (offline).

Chain: 👍/👎 inline button → callback data → ``save_feedback`` (idempotent, last vote
wins) → ``feedback_summary`` → ``GET /api/feedback/summary`` → dashboard row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from argus.api.app import create_app
from argus.interface.feedback import encode_feedback, feedback_keyboard, parse_feedback
from argus.storage.base import FeedbackSummary
from argus.storage.sqlite import SqliteStore

NOW = datetime(2026, 6, 11, 12, 0, tzinfo=UTC)


# ── callback data (pure) ─────────────────────────────────────────────────────


def test_encode_parse_roundtrip() -> None:
    data = encode_feedback("analysis", 42, symbol="BTCUSDT", severity="high", useful=True)
    vote = parse_feedback(data)
    assert vote is not None
    assert vote.ref_type == "analysis" and vote.ref_id == "42"
    assert vote.symbol == "BTCUSDT" and vote.severity == "high"
    assert vote.useful is True
    assert len(data.encode("utf-8")) <= 64  # Telegram callback_data limit


def test_encode_long_symbol_stays_within_limit() -> None:
    data = encode_feedback(
        "analysis",
        999999,
        symbol="ÆVERYLONGSYMBOLNAMEFORTESTING" * 3,
        severity="critical",
        useful=False,
    )
    assert len(data.encode("utf-8")) <= 64
    vote = parse_feedback(data)
    assert vote is not None and vote.useful is False


def test_parse_rejects_foreign_or_malformed_data() -> None:
    # Callback data we did not produce is not treated as a command → None (silently).
    assert parse_feedback(None) is None
    assert parse_feedback("") is None
    assert parse_feedback("other|thing") is None
    assert parse_feedback("fb|analysis|1|X|high|bad") is None  # malformed flag
    assert parse_feedback("fb|analysis|1") is None  # missing fields


def test_keyboard_has_two_buttons_one_row() -> None:
    kb = feedback_keyboard("analysis", 7, symbol="THYAO")
    rows = kb["inline_keyboard"]
    assert len(rows) == 1 and len(rows[0]) == 2  # a single 👍/👎 row — keeps the message compact
    up = parse_feedback(rows[0][0]["callback_data"])
    down = parse_feedback(rows[0][1]["callback_data"])
    assert up is not None and up.useful is True
    assert down is not None and down.useful is False


# ── store: idempotent vote + summary ─────────────────────────────────────────


def test_save_feedback_last_vote_wins() -> None:
    store = SqliteStore()
    store.save_feedback("analysis", "1", "user-a", useful=True, symbol="BTCUSDT", created_at=NOW)
    store.save_feedback(
        "analysis",
        "1",
        "user-a",
        useful=False,
        symbol="BTCUSDT",
        created_at=NOW,
    )  # changed their mind
    s = store.feedback_summary(now=NOW)
    assert s.total == 1  # one vote (REPLACE) — the same user is not counted twice
    assert s.useful == 0  # the last vote wins
    store.close()


def test_feedback_summary_breakdowns_and_window() -> None:
    store = SqliteStore()
    store.save_feedback(
        "analysis", "1", "u1", useful=True, symbol="BTCUSDT", severity="high", created_at=NOW
    )
    store.save_feedback(
        "analysis", "1", "u2", useful=False, symbol="BTCUSDT", severity="high", created_at=NOW
    )
    store.save_feedback(
        "alert", "2", "u1", useful=True, symbol="THYAO", severity="medium", created_at=NOW
    )
    s = store.feedback_summary(now=NOW)
    assert (s.useful, s.total) == (2, 3)
    assert s.useful_pct == 66.7
    assert s.by_symbol["BTCUSDT"].total == 2 and s.by_symbol["BTCUSDT"].useful == 1
    assert s.by_severity["medium"].useful == 1
    # Window: looking from more than 30 days later, the votes fall outside the window.
    later = store.feedback_summary(days=30, now=NOW + timedelta(days=40))
    assert later.total == 0 and later.useful_pct is None  # a zero is not fabricated
    store.close()


# ── API: /api/feedback/summary ───────────────────────────────────────────────


def _fb_client(reader: object | None) -> TestClient:
    return TestClient(create_app(feedback_provider=lambda: reader))  # type: ignore[arg-type,return-value]


def test_feedback_summary_endpoint() -> None:
    store = SqliteStore()
    store.save_feedback("analysis", "1", "u1", useful=True, symbol="BTCUSDT", severity="high")
    r = _fb_client(store).get("/api/feedback/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["useful"] == 1 and body["total"] == 1
    assert body["useful_pct"] == 100.0
    assert body["by_symbol"]["BTCUSDT"] == {"useful": 1, "total": 1}
    store.close()


def test_feedback_summary_503_without_store() -> None:
    r = _fb_client(None).get("/api/feedback/summary")
    assert r.status_code == 503
    assert r.json()["detail"] == "Feedback store not connected — DATA PENDING"


def test_feedback_summary_requires_auth() -> None:
    from argus.api.security import SessionConfig, hash_password

    app = create_app(
        feedback_provider=lambda: FeedbackSummaryFake(),  # type: ignore[arg-type]
        users={"ada": hash_password("p")},
        session=SessionConfig(secret=b"s"),
        secure_cookie=False,
    )
    assert TestClient(app).get("/api/feedback/summary").status_code == 401


# ── API: POST /api/feedback (web vote — same store as Telegram) ───────────────


def test_post_feedback_writes_and_summary_reads_it() -> None:
    # A vote written via the web POST is read back from the summary endpoint (one store).
    store = SqliteStore()
    client = TestClient(
        create_app(
            feedback_provider=lambda: store,  # type: ignore[arg-type]
            feedback_writer_provider=lambda: store,  # type: ignore[arg-type]
        )
    )
    r = client.post(
        "/api/feedback",
        json={"ref_type": "analysis", "ref_id": "7", "useful": True, "symbol": "BTCUSDT"},
    )
    assert r.status_code == 200 and r.json() == {"ok": True, "useful": True}
    summary = client.get("/api/feedback/summary").json()
    assert summary["useful"] == 1 and summary["total"] == 1
    assert summary["by_symbol"]["BTCUSDT"] == {"useful": 1, "total": 1}
    store.close()


def test_post_feedback_last_vote_wins_idempotent() -> None:
    # A second vote on the same record keeps a single row — the web user is not counted twice.
    store = SqliteStore()
    client = TestClient(
        create_app(
            feedback_provider=lambda: store,  # type: ignore[arg-type]
            feedback_writer_provider=lambda: store,  # type: ignore[arg-type]
        )
    )
    base = {"ref_type": "analysis", "ref_id": "9", "symbol": "THYAO"}
    client.post("/api/feedback", json={**base, "useful": True})
    client.post("/api/feedback", json={**base, "useful": False})
    summary = client.get("/api/feedback/summary").json()
    assert summary["total"] == 1 and summary["useful"] == 0  # the last vote wins
    store.close()


def test_post_feedback_503_without_writer() -> None:
    r = TestClient(create_app(feedback_writer_provider=lambda: None)).post(
        "/api/feedback", json={"ref_id": "1", "useful": True}
    )
    assert r.status_code == 503


def test_post_feedback_requires_auth() -> None:
    from argus.api.security import SessionConfig, hash_password

    app = create_app(
        feedback_writer_provider=lambda: SqliteStore(),  # type: ignore[arg-type]
        users={"ada": hash_password("p")},
        session=SessionConfig(secret=b"s"),
        secure_cookie=False,
    )
    r = TestClient(app).post("/api/feedback", json={"ref_id": "1", "useful": True})
    assert r.status_code == 401


class FeedbackSummaryFake:
    def feedback_summary(self, *, days: int = 30, now: datetime | None = None) -> FeedbackSummary:
        return FeedbackSummary(useful=0, total=0, days=days, by_symbol={}, by_severity={})
