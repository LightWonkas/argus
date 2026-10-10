"""SqliteStore hit rate v2 tests — migration, proposal journal, bucket aggregation.

Backward-compatibility contract: legacy ``outcome`` rows are treated as close×system;
existing data is never deleted; ``hit_rate`` (v1) counts only the decisive verdicts of
analysis×close×system.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from argus.core.contracts import AnalysisOutput, OpinionRef, Proposal
from argus.core.enums import EvidenceTier, Stance, Timeframe
from argus.storage.sqlite import SqliteStore


def _analysis(symbol: str = "BTCUSDT") -> AnalysisOutput:
    return AnalysisOutput(
        symbol=symbol,
        timeframe=Timeframe.D1,
        summary="test",
        bias="bullish",
        evidence_tier=EvidenceTier.A,
        confidence_pct=70,
        invalidation="x",
        model="m",
    )


def _proposal(symbol: str = "BTCUSDT", direction: Stance = Stance.BULLISH) -> Proposal:
    return Proposal(
        symbol=symbol,
        timeframe=Timeframe.D1,
        direction=direction,
        strength=70,
        confluence_score=80,
        rationale="test",
        contributors=(OpinionRef(agent="technical", stance=direction, confidence_pct=70),),
    )


# ── Migration: legacy schema → v2 columns, no data loss ─────────────────────


def test_migration_adds_columns_and_backfills_verdict(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    raw = sqlite3.connect(db)
    # Legacy outcome schema (no metric/variant/verdict/ref_type columns).
    raw.executescript(
        """
        CREATE TABLE analysis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL, timeframe TEXT NOT NULL, confidence_pct INTEGER NOT NULL,
            bias TEXT, evidence_tier TEXT NOT NULL, model TEXT NOT NULL,
            created_at TEXT NOT NULL, payload TEXT NOT NULL, signals TEXT NOT NULL
        );
        CREATE TABLE outcome (
            analysis_id INTEGER NOT NULL, correct INTEGER NOT NULL,
            note TEXT NOT NULL DEFAULT '', recorded_at TEXT NOT NULL
        );
        INSERT INTO outcome VALUES (1, 1, 'legacy hit', '2026-01-01T00:00:00+00:00');
        INSERT INTO outcome VALUES (2, 0, 'legacy miss', '2026-01-02T00:00:00+00:00');
        """
    )
    raw.commit()
    raw.close()

    store = SqliteStore(db)  # the migration runs here
    cols = {r[1] for r in store._conn.execute("PRAGMA table_info(outcome)")}
    assert {"metric", "variant", "verdict", "ref_type"} <= cols
    rows = list(store._conn.execute("SELECT * FROM outcome ORDER BY analysis_id"))
    # Legacy rows count as close/system/analysis; verdict is derived from the correct flag.
    assert rows[0]["metric"] == "close" and rows[0]["variant"] == "system"
    assert rows[0]["ref_type"] == "analysis" and rows[0]["verdict"] == "correct"
    assert rows[1]["verdict"] == "incorrect"
    assert rows[1]["note"] == "legacy miss"  # no data loss
    hr = store.hit_rate()
    assert (hr.correct, hr.total) == (1, 2)
    store.close()


def test_migration_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "v2.db"
    SqliteStore(db).close()
    store = SqliteStore(db)  # second open — ALTER must not fail
    assert store.hit_rate().total == 0
    store.close()


# ── Proposal journal ─────────────────────────────────────────────────────────


def test_save_proposal_and_pending_idempotent() -> None:
    store = SqliteStore()
    p1 = store.save_proposal(_proposal(), source="watch")
    p2 = store.save_proposal(_proposal("THYAO"), source="analyze")
    pending = store.pending_proposal_outcomes()
    assert [p.id for p in pending] == [p1, p2]
    assert pending[0].direction == "bullish" and pending[0].source == "watch"
    assert pending[1].symbol == "THYAO"
    latest = store.latest_proposal("BTCUSDT")
    assert latest is not None and latest == _proposal()

    # A proposal with a recorded outcome leaves the queue (LEFT JOIN, idempotent).
    store.record_outcome(p1, correct=True, metric="touch", ref_type="proposal")
    assert [p.id for p in store.pending_proposal_outcomes()] == [p2]
    store.close()


# ── hit_rate v1 isolation + v2 buckets ───────────────────────────────────────


def test_hit_rate_v1_ignores_v2_rows() -> None:
    """The v1 counter only sees decisive analysis×close×system verdicts."""
    store = SqliteStore()
    a1 = store.record_analysis(_analysis())
    store.record_outcome(a1, correct=True)  # v1-compatible row (close/system)
    # Rows that must not leak in:
    store.record_outcome(a1, correct=True, metric="touch")  # different metric
    store.record_outcome(a1, correct=True, metric="close", variant="random")  # baseline
    store.record_outcome(9, correct=True, ref_type="proposal")  # proposal series
    store.record_outcome(a1, correct=False, verdict="skip")  # indecisive (not scored)

    hr = store.hit_rate()
    assert (hr.correct, hr.total) == (1, 1)
    store.close()


def test_hit_rate_v2_buckets() -> None:
    store = SqliteStore()
    a1 = store.record_analysis(_analysis())
    store.record_outcome(a1, correct=True, metric="touch")
    store.record_outcome(a1, correct=False, metric="close")
    store.record_outcome(a1, correct=True, metric="touch", variant="random")
    store.record_outcome(5, correct=True, metric="touch", ref_type="proposal")
    store.record_outcome(a1, correct=False, metric="close", verdict="skip")  # not counted

    buckets = {(b.ref_type, b.metric, b.variant): b for b in store.hit_rate_v2()}
    assert buckets[("analysis", "touch", "system")].correct == 1
    assert buckets[("analysis", "close", "system")].total == 1
    assert buckets[("analysis", "touch", "random")].rate == 1.0
    assert buckets[("proposal", "touch", "system")].total == 1
    # The skip row entered no bucket: the close×system total stayed at 1.
    assert buckets[("analysis", "close", "system")].correct == 0
    store.close()
