"""SqliteStore — SQLite (stdlib) implementation of the memory layers.

Time series (PIT), analysis/decision journal and self-monitoring (hit rate). Decimal
values are stored as **TEXT** (precision is preserved). Datetimes are ISO-8601 (UTC).
Fully testable with ``:memory:``; move to PostgreSQL+TimescaleDB when scale requires it.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Concatenate, ParamSpec, Protocol, TypeVar, cast

from argus.core.contracts import (
    AnalysisOutput,
    Disclosure,
    NewsItem,
    OHLCVBar,
    OHLCVSeries,
    Proposal,
)
from argus.core.enums import DataRepresentation, MarketType, Timeframe
from argus.core.schemas import DataStatus, Provenance, utcnow
from argus.platform.adjust import CorporateAction
from argus.platform.triggers import TriggerSignal
from argus.storage.base import (
    AlertRecord,
    ChatTurn,
    FeedbackCount,
    FeedbackSummary,
    HitRate,
    NewsRecord,
    OutcomeBucket,
    StoredAnalysis,
    StoredProposal,
)

if TYPE_CHECKING:
    from argus.config.models import RetentionConfig
    from argus.core.crypto import (
        CryptoMarketRecord,
        OrderBookSnapshot,
        RiskAssessment,
        ShadowFill,
        ShadowOrder,
        ShadowPosition,
        Trade,
        TradeCandidate,
    )
    from argus.core.intelligence import (
        CoinResearchResult,
        CryptoNewsRecord,
        ExternalTechnicalSignal,
        MissionDailyCheckpoint,
        NewsMarketReaction,
        NormalizedCryptoEvent,
        RawNewsDocument,
        SourceRegistryEntry,
        TradePlan,
    )
    from argus.core.trading import (
        BookMetricRollup1m,
        CommissionSchedule,
        LeaderLease,
        MissionAuditEvent,
        MissionGuardResult,
        MissionLedgerEntry,
        MissionLedgerSnapshot,
        OrderIntent,
        RetentionRunReport,
        StorageHealthSnapshot,
        SymbolResearchScore,
        TradeRollup1m,
        TradingMission,
        UniverseEligibilitySnapshot,
    )

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ohlcv (
    symbol TEXT NOT NULL, timeframe TEXT NOT NULL, ts TEXT NOT NULL,
    open TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL,
    close TEXT NOT NULL, volume TEXT NOT NULL,
    source TEXT NOT NULL, confidence REAL NOT NULL, ingested_at TEXT NOT NULL,
    exchange TEXT, market_type TEXT, sequence_start INTEGER, sequence_end INTEGER,
    data_status TEXT NOT NULL DEFAULT 'ok',
    representation TEXT NOT NULL DEFAULT 'transformed',
    PRIMARY KEY (symbol, timeframe, ts)
);
CREATE TABLE IF NOT EXISTS analysis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL, timeframe TEXT NOT NULL, confidence_pct INTEGER NOT NULL,
    bias TEXT, evidence_tier TEXT NOT NULL, model TEXT NOT NULL,
    created_at TEXT NOT NULL, payload TEXT NOT NULL, signals TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outcome (
    analysis_id INTEGER NOT NULL, correct INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '', recorded_at TEXT NOT NULL,
    metric TEXT NOT NULL DEFAULT 'close',
    variant TEXT NOT NULL DEFAULT 'system',
    verdict TEXT NOT NULL DEFAULT '',
    ref_type TEXT NOT NULL DEFAULT 'analysis',
    FOREIGN KEY (analysis_id) REFERENCES analysis(id)
);
CREATE TABLE IF NOT EXISTS proposal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL, timeframe TEXT NOT NULL, created_at TEXT NOT NULL,
    direction TEXT NOT NULL, strength INTEGER NOT NULL, confluence_pct INTEGER NOT NULL,
    contributors TEXT NOT NULL DEFAULT '[]', source TEXT NOT NULL DEFAULT 'watch',
    payload TEXT
);
CREATE TABLE IF NOT EXISTS macro (
    key TEXT PRIMARY KEY,
    value REAL NOT NULL,
    trend REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'evds',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS corporate_actions (
    symbol TEXT NOT NULL, date TEXT NOT NULL,
    factor REAL NOT NULL, kind TEXT NOT NULL DEFAULT 'split',
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS feedback (
    ref_type TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    useful INTEGER NOT NULL,
    symbol TEXT,
    severity TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (ref_type, ref_id, user_id)
);
CREATE TABLE IF NOT EXISTS cooldown (
    key TEXT PRIMARY KEY, last_emit TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS news (
    source TEXT NOT NULL, ext_id TEXT NOT NULL,
    kind TEXT NOT NULL, title TEXT NOT NULL, url TEXT,
    symbol TEXT, published_at TEXT NOT NULL, ingested_at TEXT NOT NULL,
    sentiment REAL,
    importance TEXT,
    relevant INTEGER,
    target_agent TEXT,
    confidence_pct INTEGER,
    route_status TEXT,
    routed_at TEXT,
    routed_model TEXT,
    PRIMARY KEY (source, ext_id)
);
CREATE TABLE IF NOT EXISTS checkpoint (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_cost (
    day TEXT NOT NULL, role TEXT NOT NULL,
    tokens_usd REAL NOT NULL DEFAULT 0,
    search_usd REAL NOT NULL DEFAULT 0,
    calls INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, role)
);
CREATE TABLE IF NOT EXISTS chat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS crypto_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_type TEXT NOT NULL,
    dedup_key TEXT NOT NULL,
    exchange TEXT NOT NULL,
    market_type TEXT NOT NULL,
    symbol TEXT NOT NULL,
    event_ts TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL NOT NULL,
    freshness TEXT NOT NULL,
    sequence INTEGER,
    data_status TEXT NOT NULL,
    representation TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE(record_type, dedup_key)
);
CREATE INDEX IF NOT EXISTS idx_crypto_event_lookup
    ON crypto_event(symbol, market_type, record_type, event_ts);
CREATE TABLE IF NOT EXISTS trade_candidate (
    candidate_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_assessment (
    id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id TEXT NOT NULL,
    assessed_at TEXT NOT NULL, decision TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shadow_order (
    order_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, symbol TEXT NOT NULL,
    submitted_at TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shadow_fill (
    fill_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, symbol TEXT NOT NULL,
    filled_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shadow_position (
    position_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, symbol TEXT NOT NULL,
    status TEXT NOT NULL, updated_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS crypto_artifact (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_type TEXT NOT NULL,
    symbol TEXT NOT NULL,
    as_of TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    data_status TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE(artifact_type, symbol, as_of)
);
CREATE INDEX IF NOT EXISTS idx_crypto_artifact_lookup
    ON crypto_artifact(symbol, artifact_type, as_of);
CREATE TABLE IF NOT EXISTS trading_mission (
    mission_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    mode TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mission_event (
    event_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mission_event_lookup
    ON mission_event(mission_id, occurred_at);
CREATE TRIGGER IF NOT EXISTS mission_event_no_update
    BEFORE UPDATE ON mission_event BEGIN
        SELECT RAISE(ABORT, 'mission_event is append-only');
    END;
CREATE TRIGGER IF NOT EXISTS mission_event_no_delete
    BEFORE DELETE ON mission_event BEGIN
        SELECT RAISE(ABORT, 'mission_event is append-only');
    END;
CREATE TABLE IF NOT EXISTS mission_ledger_snapshot (
    mission_id TEXT PRIMARY KEY,
    as_of TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mission_ledger_entry (
    entry_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mission_ledger_lookup
    ON mission_ledger_entry(mission_id, occurred_at);
CREATE TRIGGER IF NOT EXISTS mission_ledger_entry_no_update
    BEFORE UPDATE ON mission_ledger_entry BEGIN
        SELECT RAISE(ABORT, 'mission_ledger_entry is append-only');
    END;
CREATE TRIGGER IF NOT EXISTS mission_ledger_entry_no_delete
    BEFORE DELETE ON mission_ledger_entry BEGIN
        SELECT RAISE(ABORT, 'mission_ledger_entry is append-only');
    END;
CREATE TABLE IF NOT EXISTS universe_snapshot (
    symbol TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    eligible INTEGER NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(symbol, discovered_at)
);
CREATE INDEX IF NOT EXISTS idx_universe_snapshot_latest
    ON universe_snapshot(discovered_at, eligible);
CREATE TABLE IF NOT EXISTS research_snapshot (
    symbol TEXT NOT NULL,
    as_of TEXT NOT NULL,
    status TEXT NOT NULL,
    score TEXT,
    payload TEXT NOT NULL,
    PRIMARY KEY(symbol, as_of)
);
CREATE TABLE IF NOT EXISTS commission_schedule (
    symbol TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    source TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(symbol, fetched_at)
);
CREATE TABLE IF NOT EXISTS mission_guard_result (
    result_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    assessed_at TEXT NOT NULL,
    decision TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mission_guard_lookup
    ON mission_guard_result(mission_id, assessed_at);
CREATE TABLE IF NOT EXISTS order_intent (
    intent_id TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL UNIQUE,
    mission_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    submission_authorized INTEGER NOT NULL DEFAULT 0 CHECK(submission_authorized = 0),
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_order_intent_mission
    ON order_intent(mission_id, created_at);
CREATE TABLE IF NOT EXISTS crypto_news_intelligence (
    news_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    published_at TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    verification_status TEXT NOT NULL,
    data_status TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_crypto_news_latest
    ON crypto_news_intelligence(first_seen_at, published_at);
CREATE TABLE IF NOT EXISTS news_market_reaction (
    reaction_id TEXT PRIMARY KEY,
    news_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    as_of TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE(news_id, symbol, as_of)
);
CREATE INDEX IF NOT EXISTS idx_news_reaction_latest
    ON news_market_reaction(symbol, as_of);
CREATE TABLE IF NOT EXISTS coin_research_result (
    research_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    as_of TEXT NOT NULL,
    data_status TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE(symbol, as_of)
);
CREATE INDEX IF NOT EXISTS idx_coin_research_latest
    ON coin_research_result(symbol, as_of);
CREATE TABLE IF NOT EXISTS trade_plan (
    plan_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    created_at TEXT NOT NULL,
    horizon TEXT NOT NULL,
    data_status TEXT NOT NULL,
    submission_authorized INTEGER NOT NULL DEFAULT 0 CHECK(submission_authorized = 0),
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trade_plan_latest
    ON trade_plan(mission_id, created_at);
CREATE TABLE IF NOT EXISTS mission_daily_checkpoint (
    mission_id TEXT NOT NULL,
    trading_date TEXT NOT NULL,
    as_of TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(mission_id, trading_date, as_of)
);
CREATE INDEX IF NOT EXISTS idx_mission_daily_latest
    ON mission_daily_checkpoint(mission_id, trading_date, as_of);
CREATE TABLE IF NOT EXISTS leader_lease (
    mission_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trade_rollup_1m (
    exchange TEXT NOT NULL,
    market_type TEXT NOT NULL,
    symbol TEXT NOT NULL,
    bucket_start TEXT NOT NULL,
    trade_count INTEGER NOT NULL,
    buy_quantity TEXT NOT NULL,
    sell_quantity TEXT NOT NULL,
    quote_volume TEXT NOT NULL,
    open_price TEXT NOT NULL,
    high_price TEXT NOT NULL,
    low_price TEXT NOT NULL,
    close_price TEXT NOT NULL,
    source_event_count INTEGER NOT NULL,
    PRIMARY KEY(exchange, market_type, symbol, bucket_start)
);
CREATE TABLE IF NOT EXISTS book_metric_rollup_1m (
    exchange TEXT NOT NULL,
    market_type TEXT NOT NULL,
    symbol TEXT NOT NULL,
    bucket_start TEXT NOT NULL,
    samples INTEGER NOT NULL,
    spread_sum_bps TEXT NOT NULL,
    max_spread_bps TEXT NOT NULL,
    bid_depth_sum_quote TEXT NOT NULL,
    ask_depth_sum_quote TEXT NOT NULL,
    source_event_count INTEGER NOT NULL,
    PRIMARY KEY(exchange, market_type, symbol, bucket_start)
);
CREATE TABLE IF NOT EXISTS stream_rollup_cursor (
    exchange TEXT NOT NULL,
    market_type TEXT NOT NULL,
    symbol TEXT NOT NULL,
    record_type TEXT NOT NULL,
    last_native_id TEXT NOT NULL,
    last_sequence INTEGER,
    last_event_ts TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(exchange, market_type, symbol, record_type)
);
CREATE TABLE IF NOT EXISTS retention_run (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    success INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_registry (
    source_id TEXT PRIMARY KEY,
    source_group TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    health_status TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_source_registry_group
    ON source_registry(source_group, enabled, source_id);
CREATE TABLE IF NOT EXISTS source_health (
    source_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    health_status TEXT NOT NULL,
    success INTEGER NOT NULL,
    latency_ms REAL,
    payload TEXT NOT NULL,
    PRIMARY KEY(source_id, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_source_health_latest
    ON source_health(observed_at DESC, source_id);
CREATE TABLE IF NOT EXISTS raw_news_documents (
    document_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    source_item_id TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    published_at TEXT,
    first_seen_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE(source_id, source_item_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_raw_news_pit
    ON raw_news_documents(first_seen_at DESC, document_id);
CREATE TABLE IF NOT EXISTS normalized_crypto_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    event_status TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    deduplication_key TEXT NOT NULL,
    supersedes_event_id TEXT,
    data_status TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_normalized_events_pit
    ON normalized_crypto_events(first_seen_at DESC, event_id);
CREATE INDEX IF NOT EXISTS idx_normalized_events_dedup
    ON normalized_crypto_events(deduplication_key, first_seen_at);
CREATE TABLE IF NOT EXISTS event_entities (
    event_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    confidence TEXT NOT NULL,
    ambiguous INTEGER NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(event_id, entity_type, canonical_name)
);
CREATE INDEX IF NOT EXISTS idx_event_entities_symbol
    ON event_entities(entity_type, canonical_name, event_id);
CREATE TABLE IF NOT EXISTS event_evidence (
    event_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    evidence_role TEXT NOT NULL,
    independent_origin TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(event_id, document_id)
);
CREATE TABLE IF NOT EXISTS event_market_reactions (
    reaction_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    as_of TEXT NOT NULL,
    status TEXT NOT NULL,
    data_status TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_market_reactions_latest
    ON event_market_reactions(symbol, as_of DESC, reaction_id);
CREATE TABLE IF NOT EXISTS external_technical_signals (
    signal_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    nonce TEXT NOT NULL UNIQUE,
    idempotency_key TEXT NOT NULL UNIQUE,
    submission_authorized INTEGER NOT NULL CHECK(submission_authorized=0),
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_external_signal_latest
    ON external_technical_signals(received_at DESC, signal_id);
CREATE TABLE IF NOT EXISTS guard_decisions (
    decision_id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    allowed INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_guard_decisions_subject
    ON guard_decisions(subject_type, subject_id, decided_at DESC);
CREATE TABLE IF NOT EXISTS audit_events (
    audit_id TEXT PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    subject_id TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_events_latest
    ON audit_events(occurred_at DESC, audit_id);
CREATE TRIGGER IF NOT EXISTS audit_events_no_update
BEFORE UPDATE ON audit_events BEGIN SELECT RAISE(ABORT, 'audit_events append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
BEFORE DELETE ON audit_events BEGIN SELECT RAISE(ABORT, 'audit_events append-only'); END;
CREATE TABLE IF NOT EXISTS schema_migration (
    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
);
"""

# Hit rate v2 migration: columns added to the legacy ``outcome`` table (backward compatible —
# ALTER TABLE ADD COLUMN; existing data is NOT deleted). Legacy rows count as close/system.
_OUTCOME_V2_COLUMNS: tuple[tuple[str, str], ...] = (
    ("metric", "TEXT NOT NULL DEFAULT 'close'"),
    ("variant", "TEXT NOT NULL DEFAULT 'system'"),
    ("verdict", "TEXT NOT NULL DEFAULT ''"),
    ("ref_type", "TEXT NOT NULL DEFAULT 'analysis'"),
)

# News label migration: router label columns on the ``news`` table (backward compatible).
# Legacy rows stay unlabeled (route_status NULL → "not evaluated"; unlike a failed
# classification, it was never attempted). sentiment: source sentiment (NewsSentimentAgent
# input).
_NEWS_V2_COLUMNS: tuple[tuple[str, str], ...] = (
    ("sentiment", "REAL"),
    ("importance", "TEXT"),
    ("relevant", "INTEGER"),
    ("target_agent", "TEXT"),
    ("confidence_pct", "INTEGER"),
    ("route_status", "TEXT"),
    ("routed_at", "TEXT"),
    ("routed_model", "TEXT"),
)

_PROPOSAL_V2_COLUMNS: tuple[tuple[str, str], ...] = (("payload", "TEXT"),)

_OHLCV_V2_COLUMNS: tuple[tuple[str, str], ...] = (
    ("exchange", "TEXT"),
    ("market_type", "TEXT"),
    ("sequence_start", "INTEGER"),
    ("sequence_end", "INTEGER"),
    ("data_status", "TEXT NOT NULL DEFAULT 'ok'"),
    ("representation", "TEXT NOT NULL DEFAULT 'transformed'"),
)

_CURRENT_SCHEMA_VERSION = 8


class _LockContext(Protocol):
    def __enter__(self) -> object: ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> object: ...


class _ConnectionOwner(Protocol):
    _write_lock: _LockContext


_Instance = TypeVar("_Instance")
_Params = ParamSpec("_Params")
_Result = TypeVar("_Result")
_Class = TypeVar("_Class")


def _connection_serialized(
    method: Callable[Concatenate[_Instance, _Params], _Result],
) -> Callable[Concatenate[_Instance, _Params], _Result]:
    """Serialize public calls on a SqliteStore connection across threads."""

    @wraps(method)
    def wrapped(instance: _Instance, *args: _Params.args, **kwargs: _Params.kwargs) -> _Result:
        owner = cast(_ConnectionOwner, instance)
        with owner._write_lock:
            return method(instance, *args, **kwargs)

    return cast(Callable[Concatenate[_Instance, _Params], _Result], wrapped)


def _serialize_public_connection_methods(cls: type[_Class]) -> type[_Class]:
    for name, member in tuple(vars(cls).items()):
        if name.startswith("_") or isinstance(member, (staticmethod, classmethod)):
            continue
        if callable(member):
            setattr(cls, name, _connection_serialized(member))
    return cls


@_serialize_public_connection_methods
class SqliteStore:
    """OHLCV time series + analysis journal + hit-rate tracking (SQLite)."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        # check_same_thread=False: for use from scheduler threads.
        self._closed = True
        self._path = None if str(path) == ":memory:" else Path(path).resolve()
        self._write_lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), timeout=30.0, check_same_thread=False)
        self._closed = False
        try:
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._ensure_wal()
            # The dashboard/REST and the stream may open the same file from separate processes.
            # Running DDL or a no-op UPDATE on a current schema used to take a needless write lock;
            # the version marker gives a read-only fast path. A legacy schema is upgraded only once.
            if not self._schema_is_current():
                self._conn.executescript(_SCHEMA)
                self._migrate()
                self._conn.commit()
        except Exception:
            self._conn.close()
            self._closed = True
            raise

    def _ensure_wal(self) -> None:
        """Serialize the WAL switch on a concurrent first open with bounded backoff."""
        for attempt in range(8):
            try:
                current = str(self._conn.execute("PRAGMA journal_mode").fetchone()[0])
                if current.lower() != "wal":
                    self._conn.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 7:
                    raise
                time.sleep(0.05 * (2**attempt))

    def _schema_is_current(self) -> bool:
        try:
            row = self._conn.execute(
                "SELECT 1 FROM schema_migration WHERE version=?",
                (_CURRENT_SCHEMA_VERSION,),
            ).fetchone()
        except sqlite3.OperationalError:
            return False
        return row is not None

    def _migrate(self) -> None:
        """Backward-compatible schema upgrades (outcome v2 columns and later additions).

        Legacy installs lack the v2 columns on the ``outcome`` table → they are added with
        ``ALTER TABLE ADD COLUMN``; the ``verdict`` of existing rows is derived from the
        ``correct`` flag (1→correct, 0→incorrect). No data loss; re-running is idempotent.
        """
        existing = {r["name"] for r in self._conn.execute("PRAGMA table_info(outcome)")}
        verdict_added = False
        for column, ddl in _OUTCOME_V2_COLUMNS:
            if column not in existing:
                self._conn.execute(f"ALTER TABLE outcome ADD COLUMN {column} {ddl}")
                verdict_added = verdict_added or column == "verdict"
        # Legacy rows (empty verdict): convert the correct flag into a final verdict.
        if verdict_added:
            self._conn.execute(
                "UPDATE outcome SET verdict = CASE correct "
                "WHEN 1 THEN 'correct' ELSE 'incorrect' END WHERE verdict = ''"
            )
        # News label columns (legacy rows stay NULL — unlabeled, honestly).
        news_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(news)")}
        for column, ddl in _NEWS_V2_COLUMNS:
            if column not in news_cols:
                self._conn.execute(f"ALTER TABLE news ADD COLUMN {column} {ddl}")
        proposal_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(proposal)")}
        for column, ddl in _PROPOSAL_V2_COLUMNS:
            if column not in proposal_cols:
                self._conn.execute(f"ALTER TABLE proposal ADD COLUMN {column} {ddl}")
        ohlcv_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(ohlcv)")}
        for column, ddl in _OHLCV_V2_COLUMNS:
            if column not in ohlcv_cols:
                self._conn.execute(f"ALTER TABLE ohlcv ADD COLUMN {column} {ddl}")
        self._conn.execute(
            "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (?, ?)",
            (2, utcnow().isoformat()),
        )
        self._conn.execute(
            "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (?, ?)",
            (3, utcnow().isoformat()),
        )
        self._conn.execute(
            "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (?, ?)",
            (4, utcnow().isoformat()),
        )
        self._conn.execute(
            "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (?, ?)",
            (5, utcnow().isoformat()),
        )
        self._conn.execute(
            "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (?, ?)",
            (6, utcnow().isoformat()),
        )
        self._conn.execute(
            "INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (?, ?)",
            (_CURRENT_SCHEMA_VERSION, utcnow().isoformat()),
        )

    def close(self) -> None:
        if not self._closed:
            self._conn.close()
            self._closed = True

    def ping(self) -> bool:
        """Verifies the connection can run a simple query (for health)."""
        return int(self._conn.execute("SELECT 1").fetchone()[0]) == 1

    def diagnostic_connection_state(self) -> dict[str, int | None]:
        """Cheap ownership counters; stdlib sqlite does not expose live cursor count."""

        return {
            "open_connections": 0 if self._closed else 1,
            "open_cursors": None,
        }

    def commit(self) -> None:
        """Commits the open transaction; the stream uses it for small batches."""
        self._conn.commit()

    def rollback(self) -> None:
        """Drops a failed stream batch, releasing the write lock immediately."""
        self._conn.rollback()

    def latest_market_timestamp(self) -> datetime | None:
        """Returns the newest market event time in the store; None when there is no data."""
        row = self._conn.execute(
            "SELECT MAX(ts) AS ts FROM ("
            "SELECT MAX(ts) AS ts FROM ohlcv UNION ALL "
            "SELECT MAX(event_ts) AS ts FROM crypto_event)"
        ).fetchone()
        return datetime.fromisoformat(row["ts"]) if row and row["ts"] else None

    # ── Trading mission / audit / fencing (fail-closed control plane) ───────
    def create_trading_mission(
        self,
        mission: TradingMission,
        event: MissionAuditEvent,
        *,
        ledger: MissionLedgerSnapshot | None = None,
        ledger_entry: MissionLedgerEntry | None = None,
        daily_checkpoint: MissionDailyCheckpoint | None = None,
    ) -> bool:
        """Atomically creates the mission, audit event, capital ledger and day start."""
        if event.mission_id != mission.mission_id:
            raise ValueError("mission and audit event ids do not match")
        if ledger is not None and ledger.mission_id != mission.mission_id:
            raise ValueError("mission and ledger ids do not match")
        if ledger_entry is not None and (
            ledger is None or ledger_entry.mission_id != mission.mission_id
        ):
            raise ValueError("first ledger entry does not match the mission/ledger id")
        if daily_checkpoint is not None and daily_checkpoint.mission_id != mission.mission_id:
            raise ValueError("mission and daily checkpoint ids do not match")
        with self._write_lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "INSERT INTO trading_mission "
                    "(mission_id,status,mode,created_at,updated_at,revision,payload) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        mission.mission_id,
                        mission.status.value,
                        mission.mode.value,
                        mission.created_at.isoformat(),
                        event.occurred_at.isoformat(),
                        mission.revision,
                        mission.model_dump_json(),
                    ),
                )
                self._insert_mission_event(event)
                if ledger_entry is not None:
                    self._conn.execute(
                        "INSERT INTO mission_ledger_entry "
                        "(entry_id,mission_id,kind,occurred_at,idempotency_key,payload) "
                        "VALUES (?,?,?,?,?,?)",
                        (
                            ledger_entry.entry_id,
                            ledger_entry.mission_id,
                            ledger_entry.kind.value,
                            ledger_entry.occurred_at.isoformat(),
                            ledger_entry.idempotency_key,
                            ledger_entry.model_dump_json(),
                        ),
                    )
                if ledger is not None:
                    self._conn.execute(
                        "INSERT INTO mission_ledger_snapshot(mission_id,as_of,payload) "
                        "VALUES (?,?,?)",
                        (
                            ledger.mission_id,
                            ledger.as_of.isoformat(),
                            ledger.model_dump_json(exclude_computed_fields=True),
                        ),
                    )
                if daily_checkpoint is not None:
                    self._conn.execute(
                        "INSERT INTO mission_daily_checkpoint"
                        "(mission_id,trading_date,as_of,payload) VALUES (?,?,?,?)",
                        (
                            daily_checkpoint.mission_id,
                            daily_checkpoint.trading_date.isoformat(),
                            daily_checkpoint.as_of.isoformat(),
                            daily_checkpoint.model_dump_json(),
                        ),
                    )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                self._conn.rollback()
                return False
            except Exception:
                self._conn.rollback()
                raise

    def transition_trading_mission(
        self,
        mission: TradingMission,
        event: MissionAuditEvent,
        *,
        expected_revision: int,
    ) -> None:
        """Atomic mission transition with optimistic revision + append-only event."""
        from argus.core.errors import ConcurrentStateError

        if mission.revision != expected_revision + 1:
            raise ValueError("new mission revision must be expected_revision + 1")
        if event.mission_id != mission.mission_id:
            raise ValueError("mission and audit event ids do not match")
        with self._write_lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                current = self._conn.execute(
                    "UPDATE trading_mission SET status=?,mode=?,updated_at=?,revision=?,payload=? "
                    "WHERE mission_id=? AND revision=?",
                    (
                        mission.status.value,
                        mission.mode.value,
                        event.occurred_at.isoformat(),
                        mission.revision,
                        mission.model_dump_json(),
                        mission.mission_id,
                        expected_revision,
                    ),
                )
                if current.rowcount != 1:
                    raise ConcurrentStateError("mission revision changed or mission not found")
                self._insert_mission_event(event)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _insert_mission_event(self, event: MissionAuditEvent) -> None:
        self._conn.execute(
            "INSERT INTO mission_event "
            "(event_id,mission_id,event_type,occurred_at,idempotency_key,payload) "
            "VALUES (?,?,?,?,?,?)",
            (
                event.event_id,
                event.mission_id,
                event.event_type,
                event.occurred_at.isoformat(),
                event.idempotency_key,
                event.model_dump_json(),
            ),
        )

    def load_trading_mission(self, mission_id: str) -> TradingMission | None:
        from argus.core.trading import TradingMission

        row = self._conn.execute(
            "SELECT payload FROM trading_mission WHERE mission_id=?", (mission_id,)
        ).fetchone()
        return TradingMission.model_validate_json(row["payload"]) if row else None

    def list_trading_missions(self, *, limit: int = 100) -> list[TradingMission]:
        from argus.core.trading import TradingMission

        bounded = max(1, min(limit, 500))
        rows = self._conn.execute(
            "SELECT payload FROM trading_mission ORDER BY updated_at DESC LIMIT ?", (bounded,)
        )
        return [TradingMission.model_validate_json(row["payload"]) for row in rows]

    def mission_events(self, mission_id: str, *, limit: int = 500) -> list[MissionAuditEvent]:
        from argus.core.trading import MissionAuditEvent

        bounded = max(1, min(limit, 2000))
        rows = self._conn.execute(
            "SELECT payload FROM mission_event WHERE mission_id=? "
            "ORDER BY occurred_at ASC,event_id ASC LIMIT ?",
            (mission_id, bounded),
        )
        return [MissionAuditEvent.model_validate_json(row["payload"]) for row in rows]

    def save_mission_ledger(
        self,
        snapshot: MissionLedgerSnapshot,
        *,
        entry: MissionLedgerEntry | None = None,
    ) -> bool:
        """Atomically writes the current ledger snapshot with an optional append-only entry."""
        if entry is not None and entry.mission_id != snapshot.mission_id:
            raise ValueError("ledger entry and snapshot mission ids do not match")
        with self._write_lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                inserted = True
                if entry is not None:
                    cur = self._conn.execute(
                        "INSERT OR IGNORE INTO mission_ledger_entry "
                        "(entry_id,mission_id,kind,occurred_at,idempotency_key,payload) "
                        "VALUES (?,?,?,?,?,?)",
                        (
                            entry.entry_id,
                            entry.mission_id,
                            entry.kind.value,
                            entry.occurred_at.isoformat(),
                            entry.idempotency_key,
                            entry.model_dump_json(),
                        ),
                    )
                    inserted = cur.rowcount == 1
                if inserted:
                    self._conn.execute(
                        "INSERT INTO mission_ledger_snapshot(mission_id,as_of,payload) "
                        "VALUES (?,?,?) ON CONFLICT(mission_id) DO UPDATE SET "
                        "as_of=excluded.as_of,payload=excluded.payload",
                        (
                            snapshot.mission_id,
                            snapshot.as_of.isoformat(),
                            snapshot.model_dump_json(exclude_computed_fields=True),
                        ),
                    )
                self._conn.commit()
                return inserted
            except Exception:
                self._conn.rollback()
                raise

    def load_mission_ledger(self, mission_id: str) -> MissionLedgerSnapshot | None:
        from argus.core.trading import MissionLedgerSnapshot

        row = self._conn.execute(
            "SELECT payload FROM mission_ledger_snapshot WHERE mission_id=?", (mission_id,)
        ).fetchone()
        return MissionLedgerSnapshot.model_validate_json(row["payload"]) if row else None

    def mission_ledger_entries(
        self, mission_id: str, *, limit: int = 2000
    ) -> list[MissionLedgerEntry]:
        from argus.core.trading import MissionLedgerEntry

        rows = self._conn.execute(
            "SELECT payload FROM mission_ledger_entry WHERE mission_id=? "
            "ORDER BY occurred_at ASC,entry_id ASC LIMIT ?",
            (mission_id, max(1, min(limit, 10_000))),
        )
        return [MissionLedgerEntry.model_validate_json(row["payload"]) for row in rows]

    def save_universe_snapshot(self, snapshot: UniverseEligibilitySnapshot) -> bool:
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO universe_snapshot"
            "(symbol,discovered_at,eligible,payload) VALUES (?,?,?,?)",
            (
                snapshot.symbol,
                snapshot.discovered_at.isoformat(),
                int(snapshot.eligible),
                snapshot.model_dump_json(),
            ),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def latest_universe_snapshots(self) -> list[UniverseEligibilitySnapshot]:
        from argus.core.trading import UniverseEligibilitySnapshot

        timestamp = self._conn.execute(
            "SELECT MAX(discovered_at) AS as_of FROM universe_snapshot"
        ).fetchone()["as_of"]
        if timestamp is None:
            return []
        rows = self._conn.execute(
            "SELECT payload FROM universe_snapshot WHERE discovered_at=? ORDER BY symbol",
            (timestamp,),
        )
        return [UniverseEligibilitySnapshot.model_validate_json(row["payload"]) for row in rows]

    def latest_universe_snapshot_page(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> tuple[list[UniverseEligibilitySnapshot], int, int, datetime | None, str | None]:
        """Read one bounded page from the latest PIT universe discovery batch.

        The opaque-enough cursor is the last normalized symbol because rows are immutable
        within one ``discovered_at`` batch.  Counts are computed in SQLite, avoiding the
        former full response materialization on every dashboard poll.
        """

        from argus.core.trading import UniverseEligibilitySnapshot

        bounded_limit = max(1, min(limit, 500))
        timestamp = self._conn.execute(
            "SELECT MAX(discovered_at) AS as_of FROM universe_snapshot"
        ).fetchone()["as_of"]
        if timestamp is None:
            return [], 0, 0, None, None
        counts = self._conn.execute(
            "SELECT COUNT(*) AS total,COALESCE(SUM(eligible),0) AS eligible "
            "FROM universe_snapshot WHERE discovered_at=?",
            (timestamp,),
        ).fetchone()
        normalized_cursor = cursor.strip().upper() if cursor else ""
        rows = list(
            self._conn.execute(
                "SELECT symbol,payload FROM universe_snapshot "
                "WHERE discovered_at=? AND symbol>? ORDER BY symbol LIMIT ?",
                (timestamp, normalized_cursor, bounded_limit + 1),
            )
        )
        has_more = len(rows) > bounded_limit
        page_rows = rows[:bounded_limit]
        snapshots = [
            UniverseEligibilitySnapshot.model_validate_json(row["payload"]) for row in page_rows
        ]
        next_cursor = str(page_rows[-1]["symbol"]) if has_more and page_rows else None
        return (
            snapshots,
            int(counts["total"]),
            int(counts["eligible"]),
            datetime.fromisoformat(str(timestamp)),
            next_cursor,
        )

    def save_research_snapshot(self, snapshot: SymbolResearchScore) -> bool:
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO research_snapshot"
            "(symbol,as_of,status,score,payload) VALUES (?,?,?,?,?)",
            (
                snapshot.symbol,
                snapshot.as_of.isoformat(),
                snapshot.status.value,
                str(snapshot.final_research_score)
                if snapshot.final_research_score is not None
                else None,
                snapshot.model_dump_json(),
            ),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def latest_research_scores(self, *, limit: int = 100) -> list[SymbolResearchScore]:
        from argus.core.trading import SymbolResearchScore

        rows = self._conn.execute(
            "SELECT r.payload FROM research_snapshot r "
            "JOIN (SELECT symbol,MAX(as_of) as_of FROM research_snapshot GROUP BY symbol) x "
            "ON x.symbol=r.symbol AND x.as_of=r.as_of "
            "ORDER BY CASE WHEN r.score IS NULL THEN 1 ELSE 0 END,"
            "CAST(r.score AS REAL) DESC,r.symbol "
            "LIMIT ?",
            (max(1, min(limit, 500)),),
        )
        return [SymbolResearchScore.model_validate_json(row["payload"]) for row in rows]

    def save_commission_schedule(self, schedule: CommissionSchedule) -> bool:
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO commission_schedule"
            "(symbol,fetched_at,valid_until,source,payload) VALUES (?,?,?,?,?)",
            (
                schedule.symbol,
                schedule.fetched_at.isoformat(),
                schedule.valid_until.isoformat(),
                schedule.source.value,
                schedule.model_dump_json(),
            ),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def latest_commission_schedule(self, symbol: str) -> CommissionSchedule | None:
        from argus.core.trading import CommissionSchedule

        row = self._conn.execute(
            "SELECT payload FROM commission_schedule WHERE symbol=? "
            "ORDER BY fetched_at DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        return CommissionSchedule.model_validate_json(row["payload"]) if row else None

    def commission_schedules(self, *, limit: int = 100) -> list[CommissionSchedule]:
        """Returns the newest commission record of each symbol; never invents a rate."""
        from argus.core.trading import CommissionSchedule

        rows = self._conn.execute(
            "SELECT c.payload FROM commission_schedule c "
            "JOIN (SELECT symbol,MAX(fetched_at) fetched_at FROM commission_schedule "
            "GROUP BY symbol) x ON x.symbol=c.symbol AND x.fetched_at=c.fetched_at "
            "ORDER BY c.symbol LIMIT ?",
            (max(1, min(limit, 500)),),
        )
        return [CommissionSchedule.model_validate_json(row["payload"]) for row in rows]

    def save_mission_guard_result(self, result: MissionGuardResult) -> bool:
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO mission_guard_result"
            "(result_id,mission_id,symbol,assessed_at,decision,payload) VALUES (?,?,?,?,?,?)",
            (
                result.result_id,
                result.mission_id,
                result.symbol,
                result.assessed_at.isoformat(),
                result.decision.value,
                result.model_dump_json(),
            ),
        )
        self._conn.execute(
            "INSERT OR IGNORE INTO guard_decisions"
            "(decision_id,subject_type,subject_id,decided_at,allowed,payload) "
            "VALUES (?,?,?,?,?,?)",
            (
                result.result_id,
                "trade_plan",
                f"{result.mission_id}:{result.symbol}",
                result.assessed_at.isoformat(),
                int(result.decision.value == "allow"),
                result.model_dump_json(),
            ),
        )
        self._conn.execute(
            "INSERT OR IGNORE INTO audit_events"
            "(audit_id,occurred_at,event_type,subject_id,payload) VALUES (?,?,?,?,?)",
            (
                f"guard:{result.result_id}",
                result.assessed_at.isoformat(),
                "guard_decision",
                result.result_id,
                result.model_dump_json(),
            ),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def latest_mission_guard_results(self, *, limit: int = 100) -> list[MissionGuardResult]:
        """Reads the latest GUARD decisions from the persisted record without recomputing."""
        from argus.core.trading import MissionGuardResult

        rows = self._conn.execute(
            "SELECT payload FROM mission_guard_result "
            "ORDER BY assessed_at DESC,result_id DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        )
        return [MissionGuardResult.model_validate_json(row["payload"]) for row in rows]

    def save_order_intent(self, intent: OrderIntent) -> bool:
        """Writes a non-submittable intent idempotently; a clientOrderId clash is a hard error."""
        from argus.core.errors import ConcurrentStateError

        if intent.submission_authorized:
            raise ValueError("an intent with submission authority cannot be written to this store")
        with self._write_lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO order_intent "
                    "(intent_id,client_order_id,mission_id,symbol,created_at,status,owner_id,"
                    "fencing_token,submission_authorized,payload) VALUES (?,?,?,?,?,?,?,?,0,?)",
                    (
                        intent.intent_id,
                        intent.client_order_id,
                        intent.mission_id,
                        intent.symbol,
                        intent.created_at.isoformat(),
                        intent.status.value,
                        intent.owner_id,
                        intent.fencing_token,
                        intent.model_dump_json(),
                    ),
                )
                self._conn.commit()
                return cur.rowcount == 1
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                row = self._conn.execute(
                    "SELECT intent_id,payload FROM order_intent WHERE client_order_id=?",
                    (intent.client_order_id,),
                ).fetchone()
                if row is not None and row["intent_id"] == intent.intent_id:
                    return False
                raise ConcurrentStateError(
                    "client_order_id is already used by another intent"
                ) from exc

    def order_intents(self, mission_id: str, *, limit: int = 500) -> list[OrderIntent]:
        from argus.core.trading import OrderIntent

        rows = self._conn.execute(
            "SELECT payload FROM order_intent WHERE mission_id=? ORDER BY created_at DESC LIMIT ?",
            (mission_id, max(1, min(limit, 2000))),
        )
        return [OrderIntent.model_validate_json(row["payload"]) for row in rows]

    # ── Intraday news / reaction / research / plan PIT journal ────────────
    def save_crypto_news(self, records: Sequence[CryptoNewsRecord]) -> int:
        """Writes an idempotent batch keyed by deterministic news_id; rejects full articles."""
        with self._write_lock:
            try:
                inserted = 0
                for record in records:
                    cur = self._conn.execute(
                        "INSERT OR IGNORE INTO crypto_news_intelligence"
                        "(news_id,provider,published_at,first_seen_at,verification_status,"
                        "data_status,payload) VALUES (?,?,?,?,?,?,?)",
                        (
                            record.news_id,
                            record.provider,
                            record.published_at.isoformat(),
                            record.first_seen_at.isoformat(),
                            record.verification_status.value,
                            record.data_status.value,
                            record.model_dump_json(),
                        ),
                    )
                    inserted += cur.rowcount
                self._conn.commit()
                return inserted
            except Exception:
                self._conn.rollback()
                raise

    def latest_crypto_news(self, *, limit: int = 100) -> list[CryptoNewsRecord]:
        from argus.core.intelligence import CryptoNewsRecord

        rows = self._conn.execute(
            "SELECT payload FROM crypto_news_intelligence "
            "ORDER BY first_seen_at DESC,published_at DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        )
        return [CryptoNewsRecord.model_validate_json(row["payload"]) for row in rows]

    # ── Intelligence v2 registry / raw document / normalized event PIT ────
    def save_source_registry(
        self,
        entries: Sequence[SourceRegistryEntry],
        *,
        observed_at: datetime | None = None,
    ) -> int:
        """Updates registry definitions and appends health observations as PIT."""
        at = observed_at or utcnow()
        with self._write_lock:
            try:
                changed = 0
                for entry in entries:
                    cur = self._conn.execute(
                        "INSERT INTO source_registry"
                        "(source_id,source_group,enabled,health_status,updated_at,payload) "
                        "VALUES (?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET "
                        "source_group=excluded.source_group,enabled=excluded.enabled,"
                        "health_status=excluded.health_status,updated_at=excluded.updated_at,"
                        "payload=excluded.payload",
                        (
                            entry.source_id,
                            entry.source_group.value,
                            int(entry.enabled),
                            entry.health_status.value,
                            at.isoformat(),
                            entry.model_dump_json(),
                        ),
                    )
                    changed += cur.rowcount
                    self._conn.execute(
                        "INSERT OR IGNORE INTO source_health"
                        "(source_id,observed_at,health_status,success,latency_ms,payload) "
                        "VALUES (?,?,?,?,?,?)",
                        (
                            entry.source_id,
                            at.isoformat(),
                            entry.health_status.value,
                            int(entry.last_success_at is not None),
                            entry.fetch_latency_ms,
                            entry.model_dump_json(),
                        ),
                    )
                self._conn.commit()
                return changed
            except Exception:
                self._conn.rollback()
                raise

    def source_registry_entries(self, *, limit: int = 200) -> list[SourceRegistryEntry]:
        from argus.core.intelligence import SourceRegistryEntry

        rows = self._conn.execute(
            "SELECT payload FROM source_registry ORDER BY source_group,source_id LIMIT ?",
            (max(1, min(limit, 500)),),
        )
        return [SourceRegistryEntry.model_validate_json(row["payload"]) for row in rows]

    def source_health_history(
        self, *, source_id: str | None = None, limit: int = 200
    ) -> list[SourceRegistryEntry]:
        from argus.core.intelligence import SourceRegistryEntry

        bounded = max(1, min(limit, 500))
        if source_id is None:
            rows = self._conn.execute(
                "SELECT payload FROM source_health ORDER BY observed_at DESC,source_id LIMIT ?",
                (bounded,),
            )
        else:
            rows = self._conn.execute(
                "SELECT payload FROM source_health WHERE source_id=? "
                "ORDER BY observed_at DESC LIMIT ?",
                (source_id, bounded),
            )
        return [SourceRegistryEntry.model_validate_json(row["payload"]) for row in rows]

    def save_raw_news_documents(self, documents: Sequence[RawNewsDocument]) -> int:
        with self._write_lock:
            try:
                inserted = 0
                for document in documents:
                    cur = self._conn.execute(
                        "INSERT OR IGNORE INTO raw_news_documents"
                        "(document_id,source_id,source_item_id,canonical_url,content_hash,"
                        "published_at,first_seen_at,fetched_at,payload) VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            document.document_id,
                            document.source_id,
                            document.source_item_id,
                            document.canonical_url,
                            document.content_hash,
                            document.published_at.isoformat() if document.published_at else None,
                            document.first_seen_at.isoformat(),
                            document.fetched_at.isoformat(),
                            document.model_dump_json(),
                        ),
                    )
                    inserted += cur.rowcount
                self._conn.commit()
                return inserted
            except Exception:
                self._conn.rollback()
                raise

    def latest_raw_news_documents(self, *, limit: int = 100) -> list[RawNewsDocument]:
        from argus.core.intelligence import RawNewsDocument

        rows = self._conn.execute(
            "SELECT payload FROM raw_news_documents "
            "ORDER BY first_seen_at DESC,document_id LIMIT ?",
            (max(1, min(limit, 500)),),
        )
        return [RawNewsDocument.model_validate_json(row["payload"]) for row in rows]

    def save_normalized_event(self, event: NormalizedCryptoEvent) -> bool:
        """Writes the event and its evidence/entity rows idempotently in one transaction."""
        with self._write_lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO normalized_crypto_events"
                    "(event_id,event_type,event_status,first_seen_at,expires_at,"
                    "deduplication_key,supersedes_event_id,data_status,payload) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        event.event_id,
                        event.event_type.value,
                        event.event_status.value,
                        event.first_seen_at.isoformat(),
                        event.expires_at.isoformat(),
                        event.deduplication_key,
                        event.supersedes_event_id,
                        event.data_status.value,
                        event.model_dump_json(),
                    ),
                )
                if cur.rowcount:
                    for entity in event.entities:
                        self._conn.execute(
                            "INSERT INTO event_entities"
                            "(event_id,entity_type,canonical_name,confidence,ambiguous,payload) "
                            "VALUES (?,?,?,?,?,?)",
                            (
                                event.event_id,
                                entity.entity_type,
                                entity.canonical_name,
                                str(entity.confidence),
                                int(entity.ambiguous),
                                entity.model_dump_json(),
                            ),
                        )
                    for evidence in event.evidence:
                        self._conn.execute(
                            "INSERT INTO event_evidence"
                            "(event_id,document_id,source_id,evidence_role,"
                            "independent_origin,payload) "
                            "VALUES (?,?,?,?,?,?)",
                            (
                                event.event_id,
                                evidence.document_id,
                                evidence.source_id,
                                evidence.evidence_role,
                                evidence.independent_origin,
                                evidence.model_dump_json(),
                            ),
                        )
                    self._conn.execute(
                        "INSERT INTO audit_events"
                        "(audit_id,occurred_at,event_type,subject_id,payload) VALUES (?,?,?,?,?)",
                        (
                            f"event:{event.event_id}",
                            event.first_seen_at.isoformat(),
                            "normalized_crypto_event_created",
                            event.event_id,
                            json.dumps(
                                {
                                    "event_type": event.event_type.value,
                                    "data_status": event.data_status.value,
                                    "submission_authorized": False,
                                },
                                sort_keys=True,
                            ),
                        ),
                    )
                self._conn.commit()
                return cur.rowcount == 1
            except Exception:
                self._conn.rollback()
                raise

    def normalized_event(self, event_id: str) -> NormalizedCryptoEvent | None:
        from argus.core.intelligence import NormalizedCryptoEvent

        row = self._conn.execute(
            "SELECT payload FROM normalized_crypto_events WHERE event_id=?", (event_id,)
        ).fetchone()
        return None if row is None else NormalizedCryptoEvent.model_validate_json(row["payload"])

    def latest_normalized_events(
        self,
        *,
        symbol: str | None = None,
        event_type: str | None = None,
        as_of: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[NormalizedCryptoEvent]:
        from argus.core.intelligence import NormalizedCryptoEvent

        clauses: list[str] = []
        params: list[object] = []
        if event_type:
            clauses.append("e.event_type=?")
            params.append(event_type.casefold())
        if as_of:
            clauses.append("e.first_seen_at<=?")
            params.append(as_of.isoformat())
        if symbol:
            clauses.append(
                "EXISTS (SELECT 1 FROM event_entities x WHERE x.event_id=e.event_id "
                "AND x.entity_type='symbol' AND x.canonical_name=? AND x.ambiguous=0)"
            )
            params.append(symbol.strip().upper())
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend((max(1, min(limit, 500)), max(0, offset)))
        rows = self._conn.execute(
            "SELECT e.payload FROM normalized_crypto_events e"
            + where
            + " ORDER BY e.first_seen_at DESC,e.event_id LIMIT ? OFFSET ?",
            tuple(params),
        )
        return [NormalizedCryptoEvent.model_validate_json(row["payload"]) for row in rows]

    def save_external_technical_signal(self, signal: ExternalTechnicalSignal) -> bool:
        if signal.submission_authorized:
            raise ValueError("an external signal cannot carry submission authority")
        with self._write_lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO external_technical_signals"
                "(signal_id,source_id,symbol,observed_at,received_at,nonce,idempotency_key,"
                "submission_authorized,payload) VALUES (?,?,?,?,?,?,?,0,?)",
                (
                    signal.signal_id,
                    signal.source_id,
                    signal.symbol,
                    signal.observed_at.isoformat(),
                    signal.received_at.isoformat(),
                    signal.nonce,
                    signal.idempotency_key,
                    signal.model_dump_json(),
                ),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def latest_external_technical_signals(
        self, *, limit: int = 100
    ) -> list[ExternalTechnicalSignal]:
        from argus.core.intelligence import ExternalTechnicalSignal

        rows = self._conn.execute(
            "SELECT payload FROM external_technical_signals "
            "ORDER BY received_at DESC,signal_id LIMIT ?",
            (max(1, min(limit, 500)),),
        )
        return [ExternalTechnicalSignal.model_validate_json(row["payload"]) for row in rows]

    def append_audit_event(
        self,
        event_type: str,
        payload: Mapping[str, object],
        *,
        subject_id: str | None = None,
        occurred_at: datetime | None = None,
        audit_id: str | None = None,
    ) -> str:
        at = occurred_at or utcnow()
        identity = (
            audit_id
            or hashlib.sha256(
                (
                    f"{event_type}|{subject_id or ''}|{at.isoformat()}|"
                    f"{json.dumps(payload, sort_keys=True)}"
                ).encode()
            ).hexdigest()
        )
        with self._write_lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO audit_events"
                "(audit_id,occurred_at,event_type,subject_id,payload) VALUES (?,?,?,?,?)",
                (
                    identity,
                    at.isoformat(),
                    event_type,
                    subject_id,
                    json.dumps(payload, sort_keys=True),
                ),
            )
            self._conn.commit()
        return identity

    def intelligence_health(self, *, now: datetime | None = None) -> dict[str, object]:
        at = now or utcnow()
        source_rows = self._conn.execute(
            "SELECT health_status,COUNT(*) n FROM source_registry GROUP BY health_status"
        ).fetchall()
        source_counts = {str(row["health_status"]): int(row["n"]) for row in source_rows}
        counts = {
            name: int(self._conn.execute(f"SELECT COUNT(*) n FROM {name}").fetchone()["n"])
            for name in (
                "raw_news_documents",
                "normalized_crypto_events",
                "event_market_reactions",
                "external_technical_signals",
            )
        }
        database_bytes = self._path.stat().st_size if self._path and self._path.exists() else None
        health_aggregate = self._conn.execute(
            "SELECT SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) successes,"
            "SUM(CASE WHEN health_status IN "
            "('error','unavailable','quarantined') THEN 1 ELSE 0 END) failures,"
            "MAX(CASE WHEN success=1 THEN observed_at END) last_success,AVG(latency_ms) latency "
            "FROM source_health"
        ).fetchone()
        raw_checkpoint = self.get_checkpoint("news_intelligence:health")
        checkpoint: dict[str, object] = {}
        if raw_checkpoint:
            try:
                parsed = json.loads(raw_checkpoint)
                checkpoint = parsed if isinstance(parsed, dict) else {}
            except (TypeError, ValueError, json.JSONDecodeError):
                checkpoint = {}
        audit_counts = self._conn.execute(
            "SELECT COUNT(*) rejected,"
            "SUM(CASE WHEN payload LIKE '%replay_or_duplicate%' THEN 1 ELSE 0 END) replays "
            "FROM audit_events WHERE event_type='tradingview_webhook_rejected'"
        ).fetchone()
        entity_ambiguity = int(
            self._conn.execute(
                "SELECT COUNT(*) n FROM event_entities WHERE ambiguous=1"
            ).fetchone()["n"]
        )
        events = self.latest_normalized_events(limit=500)
        duplicate_value = checkpoint.get("duplicates", 0)
        duplicate_count = int(duplicate_value) if isinstance(duplicate_value, (int, float)) else 0
        return {
            "checked_at": at.isoformat(),
            "source_status_counts": source_counts,
            "stale_source_count": source_counts.get("stale", 0),
            "unavailable_source_count": source_counts.get("unavailable", 0),
            "event_generation_count": counts["normalized_crypto_events"],
            "market_reaction_pending_count": int(
                self._conn.execute(
                    "SELECT COUNT(*) n FROM event_market_reactions WHERE data_status!='ok'"
                ).fetchone()["n"]
            ),
            "queue_depth": 0,
            "database_bytes": database_bytes,
            "source_success_count": int(health_aggregate["successes"] or 0),
            "source_failure_count": int(health_aggregate["failures"] or 0),
            "last_successful_fetch": health_aggregate["last_success"],
            "average_fetch_latency_ms": health_aggregate["latency"],
            "parse_failure_count": sum(
                entry.parse_failure_count for entry in self.source_registry_entries(limit=500)
            ),
            "duplicate_count": duplicate_count,
            "entity_ambiguity_count": entity_ambiguity,
            "corroboration_count": sum(event.corroboration_count for event in events),
            "processing_latency_ms": checkpoint.get("processing_latency_ms"),
            "retention_status": (
                "configured" if checkpoint or counts["raw_news_documents"] else "no_data"
            ),
            "webhook_rejection_count": int(audit_counts["rejected"] or 0),
            "replay_attempt_count": int(audit_counts["replays"] or 0),
            **counts,
        }

    def run_news_retention(
        self, config: RetentionConfig, *, now: datetime | None = None
    ) -> dict[str, object]:
        """Deletes old raw documents not referenced as evidence, in a bounded transaction."""
        at = now or utcnow()
        if not config.enabled:
            return {"status": "disabled", "deleted": 0, "checked_at": at.isoformat()}
        cutoff = (at - timedelta(hours=config.raw_news_document_hours)).isoformat()
        batch = config.maintenance_batch_size
        with self._write_lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                rows = self._conn.execute(
                    "SELECT d.document_id FROM raw_news_documents d "
                    "WHERE d.fetched_at<? AND NOT EXISTS "
                    "(SELECT 1 FROM event_evidence e WHERE e.document_id=d.document_id) "
                    "ORDER BY d.fetched_at,d.document_id LIMIT ?",
                    (cutoff, batch),
                ).fetchall()
                ids = [str(row["document_id"]) for row in rows]
                if ids:
                    marks = ",".join("?" for _ in ids)
                    self._conn.execute(
                        f"DELETE FROM raw_news_documents WHERE document_id IN ({marks})", ids
                    )
                self._conn.commit()
                return {"status": "ok", "deleted": len(ids), "checked_at": at.isoformat()}
            except Exception:
                self._conn.rollback()
                raise

    def save_news_market_reaction(self, reaction: NewsMarketReaction) -> bool:
        with self._write_lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO news_market_reaction"
                "(reaction_id,news_id,symbol,as_of,status,payload) VALUES (?,?,?,?,?,?)",
                (
                    reaction.reaction_id,
                    reaction.news_id,
                    reaction.symbol,
                    reaction.as_of.isoformat(),
                    reaction.status.value,
                    reaction.model_dump_json(),
                ),
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO event_market_reactions"
                "(reaction_id,event_id,symbol,as_of,status,data_status,payload) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    reaction.reaction_id,
                    reaction.news_id,
                    reaction.symbol,
                    reaction.as_of.isoformat(),
                    reaction.status.value,
                    reaction.data_status.value,
                    reaction.model_dump_json(),
                ),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def latest_news_market_reactions(
        self, *, symbol: str | None = None, limit: int = 100
    ) -> list[NewsMarketReaction]:
        from argus.core.intelligence import NewsMarketReaction

        if symbol is None:
            rows = self._conn.execute(
                "SELECT payload FROM news_market_reaction "
                "ORDER BY as_of DESC,reaction_id DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            )
        else:
            rows = self._conn.execute(
                "SELECT payload FROM news_market_reaction WHERE symbol=? "
                "ORDER BY as_of DESC,reaction_id DESC LIMIT ?",
                (symbol.upper(), max(1, min(limit, 500))),
            )
        return [NewsMarketReaction.model_validate_json(row["payload"]) for row in rows]

    def save_coin_research_result(self, result: CoinResearchResult) -> bool:
        with self._write_lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO coin_research_result"
                "(research_id,symbol,as_of,data_status,payload) VALUES (?,?,?,?,?)",
                (
                    result.research_id,
                    result.symbol,
                    result.as_of.isoformat(),
                    result.data_status.value,
                    result.model_dump_json(),
                ),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def latest_coin_research_results(self, *, limit: int = 100) -> list[CoinResearchResult]:
        from argus.core.intelligence import CoinResearchResult

        rows = self._conn.execute(
            "SELECT r.payload FROM coin_research_result r "
            "JOIN (SELECT symbol,MAX(as_of) as_of FROM coin_research_result GROUP BY symbol) x "
            "ON x.symbol=r.symbol AND x.as_of=r.as_of "
            "ORDER BY r.as_of DESC,r.symbol LIMIT ?",
            (max(1, min(limit, 500)),),
        )
        return [CoinResearchResult.model_validate_json(row["payload"]) for row in rows]

    def save_trade_plan(self, plan: TradePlan) -> bool:
        if plan.submission_authorized:
            raise ValueError("a plan with submission authority cannot be saved")
        with self._write_lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO trade_plan"
                "(plan_id,mission_id,symbol,created_at,horizon,data_status,"
                "submission_authorized,payload) VALUES (?,?,?,?,?,?,0,?)",
                (
                    plan.plan_id,
                    plan.mission_id,
                    plan.symbol,
                    plan.created_at.isoformat(),
                    plan.horizon.value,
                    plan.data_status.value,
                    plan.model_dump_json(),
                ),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def latest_trade_plans(
        self, *, mission_id: str | None = None, limit: int = 100
    ) -> list[TradePlan]:
        from argus.core.intelligence import TradePlan

        if mission_id is None:
            rows = self._conn.execute(
                "SELECT payload FROM trade_plan ORDER BY created_at DESC,plan_id DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            )
        else:
            rows = self._conn.execute(
                "SELECT payload FROM trade_plan WHERE mission_id=? "
                "ORDER BY created_at DESC,plan_id DESC LIMIT ?",
                (mission_id, max(1, min(limit, 500))),
            )
        return [TradePlan.model_validate_json(row["payload"]) for row in rows]

    def save_mission_daily_checkpoint(self, checkpoint: MissionDailyCheckpoint) -> bool:
        with self._write_lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO mission_daily_checkpoint"
                "(mission_id,trading_date,as_of,payload) VALUES (?,?,?,?)",
                (
                    checkpoint.mission_id,
                    checkpoint.trading_date.isoformat(),
                    checkpoint.as_of.isoformat(),
                    checkpoint.model_dump_json(),
                ),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def latest_mission_daily_checkpoint(self, mission_id: str) -> MissionDailyCheckpoint | None:
        from argus.core.intelligence import MissionDailyCheckpoint

        row = self._conn.execute(
            "SELECT payload FROM mission_daily_checkpoint WHERE mission_id=? "
            "ORDER BY trading_date DESC,as_of DESC LIMIT 1",
            (mission_id,),
        ).fetchone()
        return MissionDailyCheckpoint.model_validate_json(row["payload"]) if row else None

    def acquire_leader_lease(
        self,
        mission_id: str,
        owner_id: str,
        *,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> LeaderLease | None:
        """Elects a single leader with BEGIN IMMEDIATE and issues a monotonic fencing token."""
        from argus.core.trading import LeaderLease

        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        at = now or utcnow()
        expires = at + timedelta(seconds=lease_seconds)
        with self._write_lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM leader_lease WHERE mission_id=?", (mission_id,)
                ).fetchone()
                if row is not None:
                    previous_expiry = datetime.fromisoformat(row["expires_at"])
                    if row["owner_id"] != owner_id and previous_expiry > at:
                        self._conn.rollback()
                        return None
                    same_active_owner = row["owner_id"] == owner_id and previous_expiry > at
                    token = (
                        int(row["fencing_token"])
                        if same_active_owner
                        else int(row["fencing_token"]) + 1
                    )
                    acquired_at = (
                        datetime.fromisoformat(row["acquired_at"]) if same_active_owner else at
                    )
                else:
                    token = 1
                    acquired_at = at
                lease = LeaderLease(
                    mission_id=mission_id,
                    owner_id=owner_id,
                    fencing_token=token,
                    acquired_at=acquired_at,
                    heartbeat_at=at,
                    expires_at=expires,
                )
                self._conn.execute(
                    "INSERT INTO leader_lease"
                    "(mission_id,owner_id,fencing_token,acquired_at,heartbeat_at,expires_at) "
                    "VALUES (?,?,?,?,?,?) ON CONFLICT(mission_id) DO UPDATE SET "
                    "owner_id=excluded.owner_id,fencing_token=excluded.fencing_token,"
                    "acquired_at=excluded.acquired_at,heartbeat_at=excluded.heartbeat_at,"
                    "expires_at=excluded.expires_at",
                    (
                        lease.mission_id,
                        lease.owner_id,
                        lease.fencing_token,
                        lease.acquired_at.isoformat(),
                        lease.heartbeat_at.isoformat(),
                        lease.expires_at.isoformat(),
                    ),
                )
                self._conn.commit()
                return lease
            except Exception:
                self._conn.rollback()
                raise

    def renew_leader_lease(
        self,
        lease: LeaderLease,
        *,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> LeaderLease | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        at = now or utcnow()
        if at >= lease.expires_at:
            return None
        renewed = lease.model_copy(
            update={"heartbeat_at": at, "expires_at": at + timedelta(seconds=lease_seconds)}
        )
        cur = self._conn.execute(
            "UPDATE leader_lease SET heartbeat_at=?,expires_at=? "
            "WHERE mission_id=? AND owner_id=? AND fencing_token=? AND expires_at>?",
            (
                renewed.heartbeat_at.isoformat(),
                renewed.expires_at.isoformat(),
                renewed.mission_id,
                renewed.owner_id,
                renewed.fencing_token,
                at.isoformat(),
            ),
        )
        self._conn.commit()
        return renewed if cur.rowcount == 1 else None

    # ── Crypto event journal ────────────────────────────────────────────────
    def save_crypto_record(self, record: CryptoMarketRecord, *, commit: bool = True) -> bool:
        """Stores an external crypto record as an idempotent, point-in-time JSON event.

        Ingestion time/quality label does not change the identity of the same exchange event.
        A provider sequence/trade/liquidation id is part of the key when present; otherwise
        the normalized content is used.
        """
        identity_payload = record.model_dump(
            mode="json",
            exclude={"ingestion_timestamp", "confidence", "freshness", "data_status"},
        )
        native_id = (
            getattr(record, "trade_id", None)
            or getattr(record, "liquidation_id", None)
            or record.sequence
        )
        identity = json.dumps(
            {
                "type": type(record).__name__,
                "exchange": record.exchange,
                "market_type": record.market_type.value,
                "symbol": record.symbol,
                "event_timestamp": record.event_timestamp.isoformat(),
                "native_id": native_id,
                "payload": identity_payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        dedup_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO crypto_event "
            "(record_type,dedup_key,exchange,market_type,symbol,event_ts,ingested_at,source,"
            "confidence,freshness,sequence,data_status,representation,payload) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                type(record).__name__,
                dedup_key,
                record.exchange,
                record.market_type.value,
                record.symbol,
                record.event_timestamp.isoformat(),
                record.ingestion_timestamp.isoformat(),
                record.source,
                record.confidence,
                record.freshness.value,
                record.sequence,
                record.data_status.value,
                record.representation.value,
                record.model_dump_json(),
            ),
        )
        if commit:
            self._conn.commit()
        return cur.rowcount == 1

    def crypto_event_count(self, *, record_type: str | None = None) -> int:
        """Record count for tests/observability; zero when there is no data."""
        if record_type is None:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM crypto_event").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM crypto_event WHERE record_type=?", (record_type,)
            ).fetchone()
        return int(row["n"])

    def latest_crypto_record(
        self,
        symbol: str,
        record_type: str,
        *,
        market_type: MarketType | None = None,
    ) -> CryptoMarketRecord | None:
        """Returns the newest point-in-time instance of an external record type.

        The type name resolves only to known contract classes; arbitrary JSON in the store
        is never turned into an application model.
        """
        from argus.core.crypto import (
            BasisSnapshot,
            FundingRate,
            FuturesTicker,
            LiquidationEvent,
            LongShortRatio,
            OpenInterest,
            OrderBookDelta,
            OrderBookSnapshot,
            SpotTicker,
            Trade,
        )

        record_types: dict[str, type[CryptoMarketRecord]] = {
            item.__name__: item
            for item in (
                SpotTicker,
                FuturesTicker,
                Trade,
                OrderBookSnapshot,
                OrderBookDelta,
                FundingRate,
                OpenInterest,
                BasisSnapshot,
                LongShortRatio,
                LiquidationEvent,
            )
        }
        model = record_types.get(record_type)
        if model is None:
            raise ValueError(f"unknown crypto record type: {record_type}")
        sql = (
            "SELECT payload FROM crypto_event WHERE symbol=? AND record_type=?"
            + (" AND market_type=?" if market_type is not None else "")
            + " ORDER BY event_ts DESC, id DESC LIMIT 1"
        )
        params: tuple[str, ...] = (symbol, record_type)
        if market_type is not None:
            params += (market_type.value,)
        row = self._conn.execute(sql, params).fetchone()
        return model.model_validate_json(row["payload"]) if row is not None else None

    def recent_crypto_records(
        self,
        symbol: str,
        record_type: str,
        *,
        limit: int = 100,
    ) -> list[CryptoMarketRecord]:
        """Returns recent crypto events newest→oldest; the limit is strictly bounded."""
        limit = max(1, min(limit, 1000))
        latest = self.latest_crypto_record(symbol, record_type)
        if latest is None:
            return []
        model = type(latest)
        rows = self._conn.execute(
            "SELECT payload FROM crypto_event WHERE symbol=? AND record_type=? "
            "ORDER BY event_ts DESC, id DESC LIMIT ?",
            (symbol, record_type, limit),
        )
        return [model.model_validate_json(row["payload"]) for row in rows]

    def save_crypto_artifact(
        self,
        *,
        artifact_type: str,
        symbol: str,
        as_of: datetime,
        data_status: DataStatus,
        payload: Mapping[str, object] | Sequence[object],
    ) -> bool:
        """Stores feature/regime/expert/backtest output as PIT and idempotent.

        This generic envelope creates no cross-layer import dependency. The caller passes its
        own typed model via ``model_dump(mode="json")``; the API only displays this record.
        """
        if not artifact_type.strip() or not symbol.strip():
            raise ValueError("artifact_type and symbol must not be empty")
        if as_of.tzinfo is None:
            raise ValueError("artifact as_of must be tz-aware")
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO crypto_artifact"
            "(artifact_type,symbol,as_of,recorded_at,data_status,payload) VALUES (?,?,?,?,?,?)",
            (
                artifact_type,
                symbol,
                as_of.isoformat(),
                utcnow().isoformat(),
                data_status.value,
                encoded,
            ),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def latest_crypto_artifact(
        self, symbol: str, artifact_type: str
    ) -> tuple[datetime, DataStatus, object] | None:
        """Reads the newest derived artifact with its timestamp and explicit data status."""
        row = self._conn.execute(
            "SELECT as_of,data_status,payload FROM crypto_artifact "
            "WHERE symbol=? AND artifact_type=? ORDER BY as_of DESC, id DESC LIMIT 1",
            (symbol, artifact_type),
        ).fetchone()
        if row is None:
            return None
        return (
            datetime.fromisoformat(row["as_of"]),
            DataStatus(row["data_status"]),
            json.loads(row["payload"]),
        )

    def save_trade_candidate(self, candidate: TradeCandidate) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO trade_candidate(candidate_id,symbol,created_at,payload) "
            "VALUES (?,?,?,?)",
            (
                candidate.candidate_id,
                candidate.symbol,
                candidate.created_at.isoformat(),
                candidate.model_dump_json(),
            ),
        )
        self._conn.commit()

    def latest_trade_candidate(self, symbol: str) -> TradeCandidate | None:
        """Returns the symbol's newest candidate recorded by the watch pipeline."""
        from argus.core.crypto import TradeCandidate

        row = self._conn.execute(
            "SELECT payload FROM trade_candidate WHERE symbol=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        return TradeCandidate.model_validate_json(row["payload"]) if row is not None else None

    def save_risk_assessment(self, assessment: RiskAssessment) -> None:
        self._conn.execute(
            "INSERT INTO risk_assessment(candidate_id,assessed_at,decision,payload) "
            "VALUES (?,?,?,?)",
            (
                assessment.candidate_id,
                assessment.assessed_at.isoformat(),
                assessment.decision.value,
                assessment.model_dump_json(),
            ),
        )
        self._conn.commit()

    def latest_risk_assessment(self, symbol: str) -> RiskAssessment | None:
        """Returns the latest GUARD decision tied to the symbol's newest candidate."""
        from argus.core.crypto import RiskAssessment

        row = self._conn.execute(
            "SELECT r.payload FROM risk_assessment r "
            "JOIN trade_candidate c ON c.candidate_id=r.candidate_id "
            "WHERE c.symbol=? ORDER BY r.assessed_at DESC, r.id DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        return RiskAssessment.model_validate_json(row["payload"]) if row is not None else None

    def save_shadow_order(self, order: ShadowOrder) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO shadow_order"
            "(order_id,candidate_id,symbol,submitted_at,status,payload) VALUES (?,?,?,?,?,?)",
            (
                order.order_id,
                order.candidate_id,
                order.symbol,
                order.submitted_at.isoformat(),
                order.status.value,
                order.model_dump_json(),
            ),
        )
        self._conn.commit()

    def save_shadow_fill(self, fill: ShadowFill) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO shadow_fill"
            "(fill_id,order_id,symbol,filled_at,payload) VALUES (?,?,?,?,?)",
            (
                fill.fill_id,
                fill.order_id,
                fill.symbol,
                fill.filled_at.isoformat(),
                fill.model_dump_json(),
            ),
        )
        self._conn.commit()

    def save_shadow_position(self, position: ShadowPosition) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO shadow_position"
            "(position_id,candidate_id,symbol,status,updated_at,payload) VALUES (?,?,?,?,?,?)",
            (
                position.position_id,
                position.candidate_id,
                position.symbol,
                position.status.value,
                utcnow().isoformat(),
                position.model_dump_json(),
            ),
        )
        self._conn.commit()

    def shadow_positions(
        self, *, open_only: bool = False, symbol: str | None = None
    ) -> list[ShadowPosition]:
        """Restores shadow positions from persisted JSON."""
        from argus.core.crypto import ShadowPosition

        sql = "SELECT payload FROM shadow_position"
        where: list[str] = []
        params: tuple[str, ...] = ()
        if open_only:
            where.append("status=?")
            params += ("open",)
        if symbol is not None:
            where.append("symbol=?")
            params += (symbol,)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at, position_id"
        return [
            ShadowPosition.model_validate_json(row["payload"])
            for row in self._conn.execute(sql, params)
        ]

    def save_stream_rollups(
        self,
        trades: Sequence[Trade],
        books: Sequence[OrderBookSnapshot],
        *,
        commit: bool = True,
    ) -> tuple[int, int]:
        """Appends live events to a compact 1m rollup without requiring raw JSON.

        A single-row persistent cursor per stream prevents the same Binance trade or book
        sequence from being added to the rollup twice during reconnect/replay.
        The cursor and the rollup are updated in the same transaction.
        """
        trade_groups: dict[tuple[str, str, str, str], dict[str, Decimal | int]] = {}
        for trade in trades:
            trade_sequence = trade.sequence
            if trade_sequence is None and trade.trade_id.isdecimal():
                trade_sequence = int(trade.trade_id)
            if not self._claim_stream_rollup_event(
                exchange=trade.exchange,
                market_type=trade.market_type.value,
                symbol=trade.symbol,
                record_type="Trade",
                native_id=trade.trade_id,
                sequence=trade_sequence,
                event_timestamp=trade.event_timestamp,
            ):
                continue
            key = (
                trade.exchange,
                trade.market_type.value,
                trade.symbol,
                self._bucket_start(trade.event_timestamp.isoformat()),
            )
            group = trade_groups.setdefault(
                key,
                {
                    "count": 0,
                    "buy": Decimal("0"),
                    "sell": Decimal("0"),
                    "quote": Decimal("0"),
                    "open": trade.price,
                    "high": trade.price,
                    "low": trade.price,
                    "close": trade.price,
                },
            )
            group["count"] = int(group["count"]) + 1
            side_key = "buy" if trade.side.value == "buy" else "sell"
            group[side_key] = Decimal(group[side_key]) + trade.quantity
            group["quote"] = Decimal(group["quote"]) + trade.price * trade.quantity
            group["high"] = max(Decimal(group["high"]), trade.price)
            group["low"] = min(Decimal(group["low"]), trade.price)
            group["close"] = trade.price
        for key, group in trade_groups.items():
            self._merge_trade_rollup(key, group)

        book_groups: dict[tuple[str, str, str, str], dict[str, Decimal | int]] = {}
        for book in books:
            if book.best_bid is None or book.best_ask is None or book.crossed:
                raise ValueError("a low-quality order book stream cannot be rolled up")
            native_id = (
                str(book.sequence)
                if book.sequence is not None
                else book.event_timestamp.isoformat()
            )
            if not self._claim_stream_rollup_event(
                exchange=book.exchange,
                market_type=book.market_type.value,
                symbol=book.symbol,
                record_type="OrderBookSnapshot",
                native_id=native_id,
                sequence=book.sequence,
                event_timestamp=book.event_timestamp,
            ):
                continue
            mid = (book.best_bid + book.best_ask) / 2
            spread = (book.best_ask - book.best_bid) / mid * Decimal("10000")
            bid_depth = sum((level.price * level.quantity for level in book.bids), Decimal("0"))
            ask_depth = sum((level.price * level.quantity for level in book.asks), Decimal("0"))
            key = (
                book.exchange,
                book.market_type.value,
                book.symbol,
                self._bucket_start(book.event_timestamp.isoformat()),
            )
            group = book_groups.setdefault(
                key,
                {
                    "samples": 0,
                    "spread_sum": Decimal("0"),
                    "max_spread": Decimal("0"),
                    "bid_sum": Decimal("0"),
                    "ask_sum": Decimal("0"),
                },
            )
            group["samples"] = int(group["samples"]) + 1
            group["spread_sum"] = Decimal(group["spread_sum"]) + spread
            group["max_spread"] = max(Decimal(group["max_spread"]), spread)
            group["bid_sum"] = Decimal(group["bid_sum"]) + bid_depth
            group["ask_sum"] = Decimal(group["ask_sum"]) + ask_depth
        for key, group in book_groups.items():
            self._merge_book_rollup(key, group)
        if commit:
            self._conn.commit()
        return len(trade_groups), len(book_groups)

    def _claim_stream_rollup_event(
        self,
        *,
        exchange: str,
        market_type: str,
        symbol: str,
        record_type: str,
        native_id: str,
        sequence: int | None,
        event_timestamp: datetime,
    ) -> bool:
        """Advance a compact monotonic cursor, returning False for a replay."""
        key = (exchange, market_type, symbol, record_type)
        row = self._conn.execute(
            "SELECT last_native_id,last_sequence,last_event_ts FROM stream_rollup_cursor "
            "WHERE exchange=? AND market_type=? AND symbol=? AND record_type=?",
            key,
        ).fetchone()
        event_ts = event_timestamp.astimezone(UTC).isoformat()
        if row is not None:
            last_sequence = row["last_sequence"]
            if sequence is not None and last_sequence is not None:
                if sequence <= int(last_sequence):
                    return False
            elif (event_ts, native_id) <= (str(row["last_event_ts"]), str(row["last_native_id"])):
                return False
        self._conn.execute(
            "INSERT OR REPLACE INTO stream_rollup_cursor "
            "(exchange,market_type,symbol,record_type,last_native_id,last_sequence,"
            "last_event_ts,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (*key, native_id, sequence, event_ts, utcnow().isoformat()),
        )
        return True

    def recent_trade_rollups(
        self,
        symbol: str,
        *,
        market_type: str = "spot",
        limit: int = 500,
    ) -> list[TradeRollup1m]:
        """Returns the newest compact trade flow chronologically for the feature pipeline."""
        from argus.core.trading import TradeRollup1m

        rows = self._conn.execute(
            "SELECT * FROM trade_rollup_1m WHERE symbol=? AND market_type=? "
            "ORDER BY bucket_start DESC LIMIT ?",
            (symbol, market_type, max(1, min(limit, 5000))),
        ).fetchall()
        rows.reverse()
        return [
            TradeRollup1m(
                exchange=row["exchange"],
                market_type=row["market_type"],
                symbol=row["symbol"],
                bucket_start=datetime.fromisoformat(row["bucket_start"]),
                trade_count=int(row["trade_count"]),
                buy_quantity=Decimal(row["buy_quantity"]),
                sell_quantity=Decimal(row["sell_quantity"]),
                quote_volume=Decimal(row["quote_volume"]),
                open_price=Decimal(row["open_price"]),
                high_price=Decimal(row["high_price"]),
                low_price=Decimal(row["low_price"]),
                close_price=Decimal(row["close_price"]),
                source_event_count=int(row["source_event_count"]),
            )
            for row in rows
        ]

    # ── Bounded raw retention + 1m rollup ──────────────────────────────────
    def storage_health(
        self,
        config: RetentionConfig,
        *,
        now: datetime | None = None,
    ) -> StorageHealthSnapshot:
        from argus.core.trading import StorageHealthSnapshot

        at = now or utcnow()
        database_bytes = self._path.stat().st_size if self._path and self._path.exists() else None
        wal_path = Path(f"{self._path}-wal") if self._path else None
        wal_bytes = wal_path.stat().st_size if wal_path and wal_path.exists() else None
        disk_root = self._path.parent if self._path else Path.cwd()
        free_disk_bytes = shutil.disk_usage(disk_root).free
        counts = {
            row["record_type"]: int(row["n"])
            for row in self._conn.execute(
                "SELECT record_type,COUNT(*) n FROM crypto_event "
                "WHERE record_type IN "
                "('Trade','OrderBookDelta','OrderBookSnapshot','SpotTicker','FuturesTicker') "
                "GROUP BY record_type"
            )
        }
        trade_cutoff = (at - timedelta(hours=config.raw_trade_hours)).isoformat()
        depth_cutoff = (at - timedelta(hours=config.raw_depth_hours)).isoformat()
        book_cutoff = (at - timedelta(hours=config.raw_book_snapshot_hours)).isoformat()
        ticker_cutoff = (at - timedelta(hours=config.raw_ticker_hours)).isoformat()
        backlog = int(
            self._conn.execute(
                "SELECT COUNT(*) n FROM crypto_event WHERE "
                "(record_type='Trade' AND event_ts<?) OR "
                "(record_type='OrderBookDelta' AND event_ts<?) OR "
                "(record_type='OrderBookSnapshot' AND event_ts<?) OR "
                "(record_type IN ('SpotTicker','FuturesTicker') AND event_ts<?)",
                (trade_cutoff, depth_cutoff, book_cutoff, ticker_cutoff),
            ).fetchone()["n"]
        )
        reasons: list[str] = []
        if not config.enabled:
            reasons.append("retention disabled")
        if database_bytes is not None and database_bytes > config.maximum_database_mb * 1024**2:
            reasons.append("database size exceeded the policy limit")
        if free_disk_bytes < config.minimum_free_disk_mb * 1024**2:
            reasons.append("free disk space is below the policy limit")
        if backlog > config.maximum_backlog_events:
            reasons.append("retention backlog exceeded the policy limit")
        return StorageHealthSnapshot(
            checked_at=at,
            database_bytes=database_bytes,
            wal_bytes=wal_bytes,
            free_disk_bytes=free_disk_bytes,
            raw_trade_events=counts.get("Trade", 0),
            raw_depth_events=counts.get("OrderBookDelta", 0) + counts.get("OrderBookSnapshot", 0),
            raw_ticker_events=counts.get("SpotTicker", 0) + counts.get("FuturesTicker", 0),
            retention_backlog_events=backlog,
            retention_enabled=config.enabled,
            healthy=not reasons,
            reasons=tuple(reasons),
        )

    def run_crypto_retention(
        self,
        config: RetentionConfig,
        *,
        now: datetime | None = None,
    ) -> RetentionRunReport:
        """Rolls up one bounded batch inside a transaction, then deletes the raw rows.

        No raw row is deleted before its rollup is written. ``VACUUM`` is deliberately not
        called, so there is no long write lock and no temporary doubling of disk usage.
        """
        from argus.core.trading import RetentionRunReport

        started = now or utcnow()
        if not config.enabled:
            report = RetentionRunReport(
                started_at=started,
                finished_at=started,
                enabled=False,
                success=True,
                reason="disabled by retention config; no rows were deleted",
            )
            self._save_retention_report(report)
            return report

        trade_cutoff = (started - timedelta(hours=config.raw_trade_hours)).isoformat()
        book_cutoff = (started - timedelta(hours=config.raw_book_snapshot_hours)).isoformat()
        depth_cutoff = (started - timedelta(hours=config.raw_depth_hours)).isoformat()
        ticker_cutoff = (started - timedelta(hours=config.raw_ticker_hours)).isoformat()
        batch = config.maintenance_batch_size
        with self._write_lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                trade_rows = self._conn.execute(
                    "SELECT id,exchange,market_type,symbol,event_ts,payload FROM crypto_event "
                    "WHERE record_type='Trade' AND event_ts<? "
                    "ORDER BY event_ts,id LIMIT ?",
                    (trade_cutoff, batch),
                ).fetchall()
                trade_buckets = self._rollup_trade_rows(trade_rows)
                self._delete_crypto_event_ids([int(row["id"]) for row in trade_rows])

                book_rows = self._conn.execute(
                    "SELECT id,exchange,market_type,symbol,event_ts,payload FROM crypto_event "
                    "WHERE record_type='OrderBookSnapshot' AND event_ts<? "
                    "ORDER BY event_ts,id LIMIT ?",
                    (book_cutoff, batch),
                ).fetchall()
                book_buckets = self._rollup_book_rows(book_rows)
                self._delete_crypto_event_ids([int(row["id"]) for row in book_rows])

                rolled_book_keys = {
                    (row["exchange"], row["market_type"], row["symbol"], row["bucket_start"])
                    for row in self._conn.execute(
                        "SELECT exchange,market_type,symbol,bucket_start FROM book_metric_rollup_1m"
                    )
                }
                delta_rows = self._conn.execute(
                    "SELECT id,exchange,market_type,symbol,event_ts FROM crypto_event "
                    "WHERE record_type='OrderBookDelta' AND event_ts<? "
                    "ORDER BY event_ts,id LIMIT ?",
                    (depth_cutoff, batch),
                ).fetchall()
                delta_ids = [
                    int(row["id"])
                    for row in delta_rows
                    if (
                        row["exchange"],
                        row["market_type"],
                        row["symbol"],
                        self._bucket_start(row["event_ts"]),
                    )
                    in rolled_book_keys
                ]
                self._delete_crypto_event_ids(delta_ids)
                ticker_rows = self._conn.execute(
                    "SELECT id FROM crypto_event "
                    "WHERE record_type IN ('SpotTicker','FuturesTicker') AND event_ts<? "
                    "ORDER BY event_ts,id LIMIT ?",
                    (ticker_cutoff, batch),
                ).fetchall()
                self._delete_crypto_event_ids([int(row["id"]) for row in ticker_rows])
                finished = max(utcnow(), started)
                report = RetentionRunReport(
                    started_at=started,
                    finished_at=finished,
                    enabled=True,
                    success=True,
                    trade_events_rolled_up=len(trade_rows),
                    trade_buckets_written=trade_buckets,
                    book_events_rolled_up=len(book_rows),
                    book_buckets_written=book_buckets,
                    trade_events_deleted=len(trade_rows),
                    book_snapshots_deleted=len(book_rows),
                    book_deltas_deleted=len(delta_ids),
                    ticker_events_deleted=len(ticker_rows),
                )
                self._save_retention_report(report, commit=False)
                self._conn.commit()
                return report
            except Exception as exc:
                self._conn.rollback()
                report = RetentionRunReport(
                    started_at=started,
                    finished_at=max(utcnow(), started),
                    enabled=True,
                    success=False,
                    reason=f"{type(exc).__name__}: {exc}",
                )
                self._save_retention_report(report)
                return report

    @staticmethod
    def _bucket_start(value: str) -> str:
        timestamp = datetime.fromisoformat(value).astimezone(UTC)
        return timestamp.replace(second=0, microsecond=0).isoformat()

    def _rollup_trade_rows(self, rows: Sequence[sqlite3.Row]) -> int:
        groups: dict[tuple[str, str, str, str], dict[str, Decimal | int]] = {}
        for row in rows:
            payload = json.loads(row["payload"])
            key = (
                str(row["exchange"]),
                str(row["market_type"]),
                str(row["symbol"]),
                self._bucket_start(row["event_ts"]),
            )
            price = Decimal(str(payload["price"]))
            quantity = Decimal(str(payload["quantity"]))
            group = groups.setdefault(
                key,
                {
                    "count": 0,
                    "buy": Decimal("0"),
                    "sell": Decimal("0"),
                    "quote": Decimal("0"),
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                },
            )
            group["count"] = int(group["count"]) + 1
            side_key = "buy" if str(payload["side"]) == "buy" else "sell"
            group[side_key] = Decimal(group[side_key]) + quantity
            group["quote"] = Decimal(group["quote"]) + price * quantity
            group["high"] = max(Decimal(group["high"]), price)
            group["low"] = min(Decimal(group["low"]), price)
            group["close"] = price

        for key, group in groups.items():
            self._merge_trade_rollup(key, group)
        return len(groups)

    def _merge_trade_rollup(
        self,
        key: tuple[str, str, str, str],
        group: dict[str, Decimal | int],
    ) -> None:
        existing = self._conn.execute(
            "SELECT * FROM trade_rollup_1m WHERE exchange=? AND market_type=? "
            "AND symbol=? AND bucket_start=?",
            key,
        ).fetchone()
        if existing is not None:
            group["count"] = int(group["count"]) + int(existing["trade_count"])
            group["buy"] = Decimal(group["buy"]) + Decimal(existing["buy_quantity"])
            group["sell"] = Decimal(group["sell"]) + Decimal(existing["sell_quantity"])
            group["quote"] = Decimal(group["quote"]) + Decimal(existing["quote_volume"])
            group["open"] = Decimal(existing["open_price"])
            group["high"] = max(Decimal(group["high"]), Decimal(existing["high_price"]))
            group["low"] = min(Decimal(group["low"]), Decimal(existing["low_price"]))
        self._conn.execute(
            "INSERT OR REPLACE INTO trade_rollup_1m VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                *key,
                int(group["count"]),
                str(group["buy"]),
                str(group["sell"]),
                str(group["quote"]),
                str(group["open"]),
                str(group["high"]),
                str(group["low"]),
                str(group["close"]),
                int(group["count"]),
            ),
        )

    def _rollup_book_rows(self, rows: Sequence[sqlite3.Row]) -> int:
        groups: dict[tuple[str, str, str, str], dict[str, Decimal | int]] = {}
        for row in rows:
            payload = json.loads(row["payload"])
            bids = [
                (Decimal(str(level["price"])), Decimal(str(level["quantity"])))
                for level in payload["bids"]
                if Decimal(str(level["quantity"])) > 0
            ]
            asks = [
                (Decimal(str(level["price"])), Decimal(str(level["quantity"])))
                for level in payload["asks"]
                if Decimal(str(level["quantity"])) > 0
            ]
            if not bids or not asks:
                raise ValueError("empty order book for rollup")
            best_bid = max(price for price, _quantity in bids)
            best_ask = min(price for price, _quantity in asks)
            if best_bid >= best_ask:
                raise ValueError("a crossed order book cannot be rolled up")
            mid = (best_bid + best_ask) / 2
            spread = (best_ask - best_bid) / mid * Decimal("10000")
            bid_depth = sum((price * quantity for price, quantity in bids), Decimal("0"))
            ask_depth = sum((price * quantity for price, quantity in asks), Decimal("0"))
            key = (
                str(row["exchange"]),
                str(row["market_type"]),
                str(row["symbol"]),
                self._bucket_start(row["event_ts"]),
            )
            group = groups.setdefault(
                key,
                {
                    "samples": 0,
                    "spread_sum": Decimal("0"),
                    "max_spread": Decimal("0"),
                    "bid_sum": Decimal("0"),
                    "ask_sum": Decimal("0"),
                },
            )
            group["samples"] = int(group["samples"]) + 1
            group["spread_sum"] = Decimal(group["spread_sum"]) + spread
            group["max_spread"] = max(Decimal(group["max_spread"]), spread)
            group["bid_sum"] = Decimal(group["bid_sum"]) + bid_depth
            group["ask_sum"] = Decimal(group["ask_sum"]) + ask_depth

        for key, group in groups.items():
            self._merge_book_rollup(key, group)
        return len(groups)

    def _merge_book_rollup(
        self,
        key: tuple[str, str, str, str],
        group: dict[str, Decimal | int],
    ) -> None:
        existing = self._conn.execute(
            "SELECT * FROM book_metric_rollup_1m WHERE exchange=? AND market_type=? "
            "AND symbol=? AND bucket_start=?",
            key,
        ).fetchone()
        if existing is not None:
            group["samples"] = int(group["samples"]) + int(existing["samples"])
            group["spread_sum"] = Decimal(group["spread_sum"]) + Decimal(existing["spread_sum_bps"])
            group["max_spread"] = max(
                Decimal(group["max_spread"]), Decimal(existing["max_spread_bps"])
            )
            group["bid_sum"] = Decimal(group["bid_sum"]) + Decimal(existing["bid_depth_sum_quote"])
            group["ask_sum"] = Decimal(group["ask_sum"]) + Decimal(existing["ask_depth_sum_quote"])
        self._conn.execute(
            "INSERT OR REPLACE INTO book_metric_rollup_1m VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                *key,
                int(group["samples"]),
                str(group["spread_sum"]),
                str(group["max_spread"]),
                str(group["bid_sum"]),
                str(group["ask_sum"]),
                int(group["samples"]),
            ),
        )

    def _delete_crypto_event_ids(self, ids: list[int]) -> None:
        for offset in range(0, len(ids), 500):
            chunk = ids[offset : offset + 500]
            if not chunk:
                continue
            placeholders = ",".join("?" for _item in chunk)
            self._conn.execute(f"DELETE FROM crypto_event WHERE id IN ({placeholders})", chunk)

    def _save_retention_report(self, report: RetentionRunReport, *, commit: bool = True) -> None:
        self._conn.execute(
            "INSERT INTO retention_run(run_id,started_at,finished_at,success,payload) "
            "VALUES (?,?,?,?,?)",
            (
                report.run_id,
                report.started_at.isoformat(),
                report.finished_at.isoformat(),
                int(report.success),
                report.model_dump_json(),
            ),
        )
        if commit:
            self._conn.commit()

    def latest_retention_report(self) -> RetentionRunReport | None:
        from argus.core.trading import RetentionRunReport

        row = self._conn.execute(
            "SELECT payload FROM retention_run ORDER BY started_at DESC,run_id DESC LIMIT 1"
        ).fetchone()
        return RetentionRunReport.model_validate_json(row["payload"]) if row else None

    def trade_rollups(self, symbol: str) -> list[TradeRollup1m]:
        from argus.core.trading import TradeRollup1m

        rows = self._conn.execute(
            "SELECT * FROM trade_rollup_1m WHERE symbol=? ORDER BY bucket_start", (symbol,)
        )
        return [
            TradeRollup1m(
                exchange=row["exchange"],
                market_type=row["market_type"],
                symbol=row["symbol"],
                bucket_start=datetime.fromisoformat(row["bucket_start"]),
                trade_count=int(row["trade_count"]),
                buy_quantity=Decimal(row["buy_quantity"]),
                sell_quantity=Decimal(row["sell_quantity"]),
                quote_volume=Decimal(row["quote_volume"]),
                open_price=Decimal(row["open_price"]),
                high_price=Decimal(row["high_price"]),
                low_price=Decimal(row["low_price"]),
                close_price=Decimal(row["close_price"]),
                source_event_count=int(row["source_event_count"]),
            )
            for row in rows
        ]

    def book_metric_rollups(self, symbol: str) -> list[BookMetricRollup1m]:
        from argus.core.trading import BookMetricRollup1m

        rows = self._conn.execute(
            "SELECT * FROM book_metric_rollup_1m WHERE symbol=? ORDER BY bucket_start",
            (symbol,),
        )
        output: list[BookMetricRollup1m] = []
        for row in rows:
            samples = int(row["samples"])
            output.append(
                BookMetricRollup1m(
                    exchange=row["exchange"],
                    market_type=row["market_type"],
                    symbol=row["symbol"],
                    bucket_start=datetime.fromisoformat(row["bucket_start"]),
                    samples=samples,
                    average_spread_bps=Decimal(row["spread_sum_bps"]) / samples,
                    max_spread_bps=Decimal(row["max_spread_bps"]),
                    average_bid_depth_quote=Decimal(row["bid_depth_sum_quote"]) / samples,
                    average_ask_depth_quote=Decimal(row["ask_depth_sum_quote"]) / samples,
                    source_event_count=int(row["source_event_count"]),
                )
            )
        return output

    def __enter__(self) -> SqliteStore:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def __del__(self) -> None:
        """Last line of defence: avoid a sqlite3 warning if the caller forgets the lifecycle."""
        if not getattr(self, "_closed", True):
            self.close()

    # ── Time series (PIT) ───────────────────────────────────────────────────
    def save_series(self, series: OHLCVSeries) -> int:
        """Stores the candles of the series (idempotent upsert); returns the rows written."""
        rows = [
            (
                series.symbol,
                series.timeframe.value,
                bar.timestamp.isoformat(),
                str(bar.open),
                str(bar.high),
                str(bar.low),
                str(bar.close),
                str(bar.volume),
                series.provenance.source,
                series.provenance.confidence,
                series.provenance.ingested_at.isoformat(),
                series.exchange,
                series.market_type.value if series.market_type is not None else None,
                series.sequence_start,
                series.sequence_end,
                series.data_status.value,
                series.representation.value,
            )
            for bar in series.bars
        ]
        self._conn.executemany(
            "INSERT OR REPLACE INTO ohlcv "
            "(symbol,timeframe,ts,open,high,low,close,volume,source,confidence,ingested_at,"
            "exchange,market_type,sequence_start,sequence_end,data_status,representation) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self._conn.commit()
        return len(rows)

    def load_bars(self, symbol: str, timeframe: Timeframe, *, limit: int = 100) -> list[OHLCVBar]:
        """Returns the newest ``limit`` candles (in ascending time order)."""
        cur = self._conn.execute(
            "SELECT * FROM ohlcv WHERE symbol=? AND timeframe=? ORDER BY ts DESC LIMIT ?",
            (symbol, timeframe.value, limit),
        )
        bars = [
            OHLCVBar(
                timestamp=datetime.fromisoformat(r["ts"]),
                open=Decimal(r["open"]),
                high=Decimal(r["high"]),
                low=Decimal(r["low"]),
                close=Decimal(r["close"]),
                volume=Decimal(r["volume"]),
            )
            for r in cur.fetchall()
        ]
        bars.reverse()  # ascending
        return bars

    def load_series(self, symbol: str, timeframe: Timeframe, *, limit: int = 100) -> OHLCVSeries:
        """Builds an OHLCVSeries from the stored candles (with provenance)."""
        cur = self._conn.execute(
            "SELECT source, confidence, exchange, market_type, sequence_start, sequence_end, "
            "data_status, representation FROM ohlcv WHERE symbol=? AND timeframe=? "
            "ORDER BY ts DESC LIMIT 1",
            (symbol, timeframe.value),
        )
        meta = cur.fetchone()
        bars = self.load_bars(symbol, timeframe, limit=limit)
        source = meta["source"] if meta else "storage"
        confidence = float(meta["confidence"]) if meta else 1.0
        timestamp = bars[-1].timestamp if bars else utcnow()
        provenance = Provenance(source=source, timestamp=timestamp, confidence=confidence)
        return OHLCVSeries(
            symbol=symbol,
            timeframe=timeframe,
            bars=tuple(bars),
            provenance=provenance,
            exchange=meta["exchange"] if meta else None,
            market_type=MarketType(meta["market_type"]) if meta and meta["market_type"] else None,
            sequence_start=int(meta["sequence_start"])
            if meta and meta["sequence_start"] is not None
            else None,
            sequence_end=int(meta["sequence_end"])
            if meta and meta["sequence_end"] is not None
            else None,
            data_status=DataStatus(meta["data_status"]) if meta else DataStatus.OK,
            representation=DataRepresentation(meta["representation"])
            if meta
            else DataRepresentation.TRANSFORMED,
        )

    # ── Analysis / decision journal ─────────────────────────────────────────
    def record_analysis(
        self, analysis: AnalysisOutput, *, signals: Sequence[TriggerSignal] = ()
    ) -> int:
        """Writes the analysis to the journal; returns the row id."""
        signal_json = json.dumps(
            [
                {"kind": s.kind.value, "severity": s.severity.value, "message": s.message}
                for s in signals
            ]
        )
        cur = self._conn.execute(
            "INSERT INTO analysis "
            "(symbol,timeframe,confidence_pct,bias,evidence_tier,model,created_at,payload,signals) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                analysis.symbol,
                analysis.timeframe.value,
                analysis.confidence_pct,
                analysis.bias,
                analysis.evidence_tier.value,
                analysis.model,
                utcnow().isoformat(),
                analysis.model_dump_json(),
                signal_json,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def recent_analyses(
        self, *, symbol: str | None = None, limit: int = 20
    ) -> list[StoredAnalysis]:
        """Returns the most recent analyses (newest→oldest)."""
        if symbol is None:
            cur = self._conn.execute(
                "SELECT id, created_at, payload FROM analysis ORDER BY id DESC LIMIT ?", (limit,)
            )
        else:
            cur = self._conn.execute(
                "SELECT id, created_at, payload FROM analysis WHERE symbol=? "
                "ORDER BY id DESC LIMIT ?",
                (symbol, limit),
            )
        return [
            StoredAnalysis(
                id=int(r["id"]),
                created_at=datetime.fromisoformat(r["created_at"]),
                analysis=AnalysisOutput.model_validate_json(r["payload"]),
            )
            for r in cur.fetchall()
        ]

    def recent_alerts(self, *, limit: int = 50) -> list[AlertRecord]:
        """Flattens recent alerts out of the journal's triggers (signals) (newest→oldest).

        There is no separate alert table; the ``signals`` column of each analysis holds the
        triggers that woke it → the panel feed is derived from them. ``limit`` is the number of
        **analyses**; one analysis may contain several triggers.
        """
        cur = self._conn.execute(
            "SELECT id, symbol, created_at, signals FROM analysis ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        alerts: list[AlertRecord] = []
        for r in cur.fetchall():
            created = datetime.fromisoformat(r["created_at"])
            for sig in json.loads(r["signals"] or "[]"):
                alerts.append(
                    AlertRecord(
                        analysis_id=int(r["id"]),
                        created_at=created,
                        symbol=r["symbol"],
                        severity=str(sig.get("severity", "")),
                        kind=str(sig.get("kind", "")),
                        message=str(sig.get("message", "")),
                    )
                )
        return alerts

    # ── News / KAP archive (deduplicated) ───────────────────────────────────
    def save_news(self, items: Sequence[NewsItem | Disclosure]) -> int:
        """Archives news/KAP disclosures (``(source, ext_id)`` dedup). Returns the new row count.

        ``NewsItem`` → kind=news, ``Disclosure`` → kind=disclosure. A record that arrives again
        is skipped (INSERT OR IGNORE) → no repeated alert/display (idempotent).
        """
        now = utcnow().isoformat()
        added = 0
        for item in items:
            ext_id: str
            kind: str
            symbol: str | None
            sentiment: float | None
            if isinstance(item, Disclosure):
                ext_id, kind = item.disclosure_id, "disclosure"
                symbol, sentiment = item.symbol, None
            else:
                ext_id, kind = item.id, "news"
                symbol = item.symbols[0] if item.symbols else None
                sentiment = item.sentiment
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO news "
                "(source, ext_id, kind, title, url, symbol, published_at, ingested_at, sentiment) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    item.provenance.source,
                    ext_id,
                    kind,
                    item.title,
                    item.url,
                    symbol,
                    item.published_at.isoformat(),
                    now,
                    sentiment,
                ),
            )
            added += cur.rowcount
        self._conn.commit()
        return added

    def recent_news(self, *, source: str | None = None, limit: int = 50) -> list[NewsRecord]:
        """Returns the most recent news/disclosures (newest→oldest); filtered by ``source``."""
        if source is None:
            cur = self._conn.execute(
                "SELECT * FROM news ORDER BY published_at DESC LIMIT ?", (limit,)
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM news WHERE source=? ORDER BY published_at DESC LIMIT ?",
                (source, limit),
            )
        return [self._news_record(r) for r in cur.fetchall()]

    @staticmethod
    def _news_record(r: sqlite3.Row) -> NewsRecord:
        return NewsRecord(
            source=r["source"],
            ext_id=r["ext_id"],
            kind=r["kind"],
            title=r["title"],
            url=r["url"],
            symbol=r["symbol"],
            published_at=datetime.fromisoformat(r["published_at"]),
            sentiment=r["sentiment"],
            importance=r["importance"],
            relevant=None if r["relevant"] is None else bool(r["relevant"]),
            target_agent=r["target_agent"],
            confidence_pct=r["confidence_pct"],
            route_status=r["route_status"],
        )

    def unrouted_news(self, *, limit: int = 50) -> list[NewsRecord]:
        """News not yet passed through the router (oldest→newest) — resumes where it left off
        after a restart (idempotent; once route_status is written a row is not returned again)."""
        cur = self._conn.execute(
            "SELECT * FROM news WHERE route_status IS NULL ORDER BY published_at ASC LIMIT ?",
            (limit,),
        )
        return [self._news_record(r) for r in cur.fetchall()]

    def update_news_route(
        self,
        source: str,
        ext_id: str,
        *,
        status: str,
        importance: str | None = None,
        relevant: bool | None = None,
        target_agent: str | None = None,
        confidence_pct: int | None = None,
        model: str = "",
    ) -> None:
        """Writes the router label to the archive. ``status != 'ok'`` → labels stay NULL (a
        classification that could not be produced is never stored as if it were valid)."""
        self._conn.execute(
            "UPDATE news SET importance=?, relevant=?, target_agent=?, confidence_pct=?, "
            "route_status=?, routed_at=?, routed_model=? WHERE source=? AND ext_id=?",
            (
                importance,
                None if relevant is None else int(relevant),
                target_agent,
                confidence_pct,
                status,
                utcnow().isoformat(),
                model,
                source,
                ext_id,
            ),
        )
        self._conn.commit()

    # ── LLM cost ledger (shared cross-process daily cap) ─────────────────────
    def add_llm_cost(self, day: str, role: str, tokens_usd: float, search_usd: float) -> None:
        """Adds the cost of one call to the day/role (upsert sum; the ledger is never deleted)."""
        self._conn.execute(
            "INSERT INTO llm_cost (day, role, tokens_usd, search_usd, calls) "
            "VALUES (?,?,?,?,1) "
            "ON CONFLICT(day, role) DO UPDATE SET "
            "tokens_usd = tokens_usd + excluded.tokens_usd, "
            "search_usd = search_usd + excluded.search_usd, "
            "calls = calls + 1",
            (day, role, tokens_usd, search_usd),
        )
        self._conn.commit()

    def llm_cost_for_day(self, day: str) -> dict[str, tuple[float, float]]:
        """Breakdown of one day: role → (tokens_usd, search_usd). Empty without records."""
        cur = self._conn.execute(
            "SELECT role, tokens_usd, search_usd FROM llm_cost WHERE day=?", (day,)
        )
        return {r["role"]: (float(r["tokens_usd"]), float(r["search_usd"])) for r in cur.fetchall()}

    def llm_cost_recent_days(
        self, *, days: int = 7, now: datetime | None = None
    ) -> list[tuple[str, float]]:
        """Totals of the last ``days`` days (oldest→newest; 0.0 for empty days) — panel trend."""
        reference = (now or utcnow()).date()
        out: list[tuple[str, float]] = []
        for i in range(days - 1, -1, -1):
            day = (reference - timedelta(days=i)).isoformat()
            cur = self._conn.execute(
                "SELECT COALESCE(SUM(tokens_usd + search_usd), 0) t FROM llm_cost WHERE day=?",
                (day,),
            )
            out.append((day, float(cur.fetchone()["t"])))
        return out

    def llm_cost_for_month(self, month_prefix: str) -> float:
        """Month total (``YYYY-MM`` prefix) — for the monthly projection."""
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(tokens_usd + search_usd), 0) t FROM llm_cost WHERE day LIKE ?",
            (f"{month_prefix}%",),
        )
        return float(cur.fetchone()["t"])

    # ── Checkpoint KV (persistent resume point for pollers/consumers) ────────
    def get_checkpoint(self, key: str) -> str | None:
        """Persistent checkpoint value (None if absent) — resume point after a restart."""
        cur = self._conn.execute("SELECT value FROM checkpoint WHERE key=?", (key,))
        row = cur.fetchone()
        return str(row["value"]) if row else None

    def set_checkpoint(self, key: str, value: str, *, commit: bool = True) -> None:
        """Persists the checkpoint value (upsert)."""
        self._conn.execute(
            "INSERT OR REPLACE INTO checkpoint (key, value, updated_at) VALUES (?,?,?)",
            (key, value, utcnow().isoformat()),
        )
        if commit:
            self._conn.commit()

    # ── Copilot chat history ────────────────────────────────────────────────
    def save_chat(self, role: str, content: str) -> int:
        """Writes a chat message (user/assistant); returns the row id."""
        cur = self._conn.execute(
            "INSERT INTO chat (role, content, created_at) VALUES (?,?,?)",
            (role, content, utcnow().isoformat()),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def recent_chat(self, *, limit: int = 50) -> list[ChatTurn]:
        """Returns recent chat messages in **oldest→newest** order (natural flow in the UI)."""
        cur = self._conn.execute(
            "SELECT id, role, content, created_at FROM chat ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = [
            ChatTurn(
                id=int(r["id"]),
                role=r["role"],
                content=r["content"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in cur.fetchall()
        ]
        return list(reversed(rows))  # oldest→newest

    # ── Confluence proposal journal ─────────────────────────────────────────
    def save_proposal(self, proposal: Proposal, *, source: str) -> int:
        """Writes a confluence proposal to the journal; returns the row id.

        ``source``: watch | analyze | api (stored values). Contributors JSON stays archived
        (transparency); the evaluator reads the lean fields.
        """
        contributors = json.dumps(
            [
                {"agent": c.agent, "stance": c.stance.value, "confidence_pct": c.confidence_pct}
                for c in proposal.contributors
            ]
        )
        cur = self._conn.execute(
            "INSERT INTO proposal "
            "(symbol,timeframe,created_at,direction,strength,confluence_pct,"
            "contributors,source,payload) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                proposal.symbol,
                proposal.timeframe.value,
                utcnow().isoformat(),
                proposal.direction.value,
                proposal.strength,
                proposal.confluence_score,
                contributors,
                source,
                proposal.model_dump_json(),
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def latest_proposal(self, symbol: str) -> Proposal | None:
        """Last full proposal actually recorded by the watch pipeline; legacy lean row → None."""
        row = self._conn.execute(
            "SELECT payload FROM proposal WHERE symbol=? AND payload IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        return Proposal.model_validate_json(row["payload"]) if row else None

    def pending_proposal_outcomes(self, *, limit: int = 200) -> list[StoredProposal]:
        """Returns proposals without a recorded outcome (oldest→newest; LEFT JOIN, idempotent)."""
        cur = self._conn.execute(
            "SELECT p.* FROM proposal p "
            "LEFT JOIN outcome o ON o.ref_type='proposal' AND o.analysis_id = p.id "
            "WHERE o.analysis_id IS NULL ORDER BY p.id ASC LIMIT ?",
            (limit,),
        )
        return [
            StoredProposal(
                id=int(r["id"]),
                symbol=r["symbol"],
                timeframe=r["timeframe"],
                created_at=datetime.fromisoformat(r["created_at"]),
                direction=r["direction"],
                strength=int(r["strength"]),
                confluence_pct=int(r["confluence_pct"]),
                source=r["source"],
            )
            for r in cur.fetchall()
        ]

    # ── Self-monitoring / hit rate ──────────────────────────────────────────
    def pending_outcomes(self, *, limit: int = 200) -> list[StoredAnalysis]:
        """Returns analyses without a recorded outcome (oldest→newest).

        Analyses with no row in the ``outcome`` table = not evaluated. Oldest→newest order: those
        whose horizon closes first are processed first. Once ``record_outcome`` writes a row
        they are not returned again (idempotent).
        """
        cur = self._conn.execute(
            "SELECT a.id, a.created_at, a.payload FROM analysis a "
            "LEFT JOIN outcome o ON o.ref_type='analysis' AND o.analysis_id = a.id "
            "WHERE o.analysis_id IS NULL ORDER BY a.id ASC LIMIT ?",
            (limit,),
        )
        return [
            StoredAnalysis(
                id=int(r["id"]),
                created_at=datetime.fromisoformat(r["created_at"]),
                analysis=AnalysisOutput.model_validate_json(r["payload"]),
            )
            for r in cur.fetchall()
        ]

    def record_outcome(
        self,
        analysis_id: int,
        *,
        correct: bool,
        note: str = "",
        metric: str = "close",
        variant: str = "system",
        ref_type: str = "analysis",
        verdict: str | None = None,
    ) -> None:
        """Records an evaluation result (v2: metric + variant + series type).

        Backward compatible: legacy calls (only ``correct``/``note``) are written as
        close/system/analysis. Without ``verdict`` it is derived from ``correct``; ``skip``
        verdicts are written with ``verdict='skip'`` (excluded from hit-rate totals but removed
        from the pending queue — deterministic verdicts are not re-evaluated forever).
        """
        final_verdict = verdict or ("correct" if correct else "incorrect")
        self._conn.execute(
            "INSERT INTO outcome "
            "(analysis_id, correct, note, recorded_at, metric, variant, verdict, ref_type) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                analysis_id,
                1 if correct else 0,
                note,
                utcnow().isoformat(),
                metric,
                variant,
                final_verdict,
                ref_type,
            ),
        )
        self._conn.commit()

    def hit_rate(self, *, symbol: str | None = None) -> HitRate:
        """Hit-rate summary from recorded outcomes (v1 series: analysis × close × system).

        v2 rows (touch metric, baselines, proposal series, skip verdicts) do **not** mix into
        this summary — the v1 meaning is preserved (StatusBar/legacy panel fields).
        """
        base_where = (
            "o.metric='close' AND o.variant='system' AND o.ref_type='analysis' "
            "AND o.verdict IN ('correct','incorrect')"
        )
        if symbol is None:
            cur = self._conn.execute(
                f"SELECT COUNT(*) n, COALESCE(SUM(o.correct),0) c FROM outcome o WHERE {base_where}"
            )
        else:
            cur = self._conn.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(o.correct),0) c FROM outcome o "
                f"JOIN analysis a ON a.id=o.analysis_id WHERE {base_where} AND a.symbol=?",
                (symbol,),
            )
        row = cur.fetchone()
        return HitRate(correct=int(row["c"]), total=int(row["n"]))

    def hit_rate_v2(self) -> list[OutcomeBucket]:
        """Hit rate v2 buckets: aggregation of final verdicts per (series, metric, variant).

        Only ``correct``/``incorrect`` verdicts are counted (skip is not scored). The API builds
        the baseline-comparison response from these buckets (`GET /api/hitrate`).
        """
        cur = self._conn.execute(
            "SELECT ref_type, metric, variant, "
            "COUNT(*) n, COALESCE(SUM(correct),0) c FROM outcome "
            "WHERE verdict IN ('correct','incorrect') "
            "GROUP BY ref_type, metric, variant"
        )
        return [
            OutcomeBucket(
                ref_type=r["ref_type"],
                metric=r["metric"],
                variant=r["variant"],
                correct=int(r["c"]),
                total=int(r["n"]),
            )
            for r in cur.fetchall()
        ]

    # ── Macro series (EVDS + F&G → Macro/Positioning agents) ────────────────────
    def save_macro(self, values: Mapping[str, float], trends: Mapping[str, float]) -> int:
        """Writes macro VALUES (key upsert); without a trend, 0 (directionless) is stored.

        The trend context (``load_macro_trends``) filters out directionless ones; value
        persistence (``load_macro_values`` — e.g. the Fear&Greed level) also covers those metrics.
        """
        now = utcnow().isoformat()
        rows = [(key, value, trends.get(key, 0.0), "evds", now) for key, value in values.items()]
        if not rows:
            return 0
        self._conn.executemany(
            "INSERT OR REPLACE INTO macro (key, value, trend, source, updated_at) "
            "VALUES (?,?,?,?,?)",
            rows,
        )
        self._conn.commit()
        return len(rows)

    def load_macro_trends(
        self, *, max_age_h: float = 48.0, now: datetime | None = None
    ) -> dict[str, float] | None:
        """Fresh + DIRECTIONAL macro trends; **None** if all are stale/directionless (NO_DATA).

        Macro moves slowly → a daily fetch is enough; a record older than ``max_age_h`` or with
        trend=0 stays out of the context (no direction is inferred from stale/directionless
        macro).
        """
        reference = now or utcnow()
        cutoff = (reference - timedelta(hours=max_age_h)).isoformat()
        cur = self._conn.execute(
            "SELECT key, trend FROM macro WHERE updated_at >= ? AND trend != 0", (cutoff,)
        )
        trends = {r["key"]: float(r["trend"]) for r in cur.fetchall()}
        return trends or None

    def load_macro_values(
        self, *, max_age_h: float = 48.0, now: datetime | None = None
    ) -> dict[str, float] | None:
        """Fresh macro VALUES (e.g. ``crypto_fng`` level 0–100); None if stale/empty."""
        reference = now or utcnow()
        cutoff = (reference - timedelta(hours=max_age_h)).isoformat()
        cur = self._conn.execute("SELECT key, value FROM macro WHERE updated_at >= ?", (cutoff,))
        values = {r["key"]: float(r["value"]) for r in cur.fetchall()}
        return values or None

    # ── Corporate actions (input for read-time adjustment) ───────────────────
    def save_corporate_actions(self, actions: Sequence[CorporateAction]) -> int:
        """Writes derived corporate actions (``(symbol, date)`` idempotent upsert)."""
        rows = [(a.symbol, a.date.isoformat(), a.factor, a.kind) for a in actions if a.factor > 0]
        if not rows:
            return 0
        self._conn.executemany(
            "INSERT OR REPLACE INTO corporate_actions (symbol, date, factor, kind) "
            "VALUES (?,?,?,?)",
            rows,
        )
        self._conn.commit()
        return len(rows)

    def load_corporate_actions(self, symbol: str) -> list[CorporateAction]:
        """Corporate actions of a symbol (date-ordered; empty if none — nothing is invented)."""
        cur = self._conn.execute(
            "SELECT symbol, date, factor, kind FROM corporate_actions "
            "WHERE symbol=? ORDER BY date ASC",
            (symbol,),
        )
        return [
            CorporateAction(
                symbol=r["symbol"],
                date=datetime.fromisoformat(r["date"]),
                factor=float(r["factor"]),
                kind=r["kind"],
            )
            for r in cur.fetchall()
        ]

    def delete_ohlcv(self, symbol: str, *, timeframe: Timeframe | None = None) -> int:
        """Deletes the OHLCV rows of a symbol; returns the number of rows deleted.

        **Irreversible** — called only with explicit user approval (raw-data migration:
        adjusted legacy rows must not mix with raw new rows).
        """
        if timeframe is None:
            cur = self._conn.execute("DELETE FROM ohlcv WHERE symbol=?", (symbol,))
        else:
            cur = self._conn.execute(
                "DELETE FROM ohlcv WHERE symbol=? AND timeframe=?", (symbol, timeframe.value)
            )
        self._conn.commit()
        return int(cur.rowcount)

    # ── User feedback (alert/analysis 👍/👎) ─────────────────────────────────
    def save_feedback(
        self,
        ref_type: str,
        ref_id: str,
        user_id: str,
        *,
        useful: bool,
        symbol: str | None = None,
        severity: str | None = None,
        created_at: datetime | None = None,
    ) -> None:
        """Records a vote — idempotent: the same user may change their mind (last vote wins)."""
        self._conn.execute(
            "INSERT OR REPLACE INTO feedback "
            "(ref_type, ref_id, user_id, useful, symbol, severity, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                ref_type,
                ref_id,
                user_id,
                1 if useful else 0,
                symbol,
                severity,
                (created_at or utcnow()).isoformat(),
            ),
        )
        self._conn.commit()

    def feedback_summary(self, *, days: int = 30, now: datetime | None = None) -> FeedbackSummary:
        """Vote summary of the last ``days`` days: useful/total + symbol/severity breakdown."""
        reference = now or utcnow()
        cutoff = (reference - timedelta(days=days)).isoformat()
        cur = self._conn.execute(
            "SELECT symbol, severity, useful FROM feedback "
            "WHERE created_at >= ? AND created_at <= ?",
            (cutoff, reference.isoformat()),
        )
        useful = total = 0
        by_symbol: dict[str, list[int]] = {}
        by_severity: dict[str, list[int]] = {}
        for r in cur.fetchall():
            total += 1
            vote = int(r["useful"])
            useful += vote
            if r["symbol"]:
                s = by_symbol.setdefault(r["symbol"], [0, 0])
                s[0] += vote
                s[1] += 1
            if r["severity"]:
                s = by_severity.setdefault(r["severity"], [0, 0])
                s[0] += vote
                s[1] += 1
        return FeedbackSummary(
            useful=useful,
            total=total,
            days=days,
            by_symbol={k: FeedbackCount(v[0], v[1]) for k, v in sorted(by_symbol.items())},
            by_severity={k: FeedbackCount(v[0], v[1]) for k, v in sorted(by_severity.items())},
        )

    # ── Cooldown persistence (idempotent recovery) ──────────────────────────
    def get_cooldown(self, key: str) -> datetime | None:
        """Last emit time for the key (recovery after a restart)."""
        cur = self._conn.execute("SELECT last_emit FROM cooldown WHERE key=?", (key,))
        row = cur.fetchone()
        return datetime.fromisoformat(row["last_emit"]) if row else None

    def set_cooldown(self, key: str, when: datetime) -> None:
        """Persists the last emit time for the key."""
        self._conn.execute(
            "INSERT OR REPLACE INTO cooldown (key, last_emit) VALUES (?, ?)",
            (key, when.isoformat()),
        )
        self._conn.commit()
