"""Telegram command bot (live glue) — python-telegram-bot is imported lazily.

Commands: ``/status`` (health), ``/analyze <SYMBOL>`` (runs the whole chain:
price → quality → trigger → LLM analysis → message). Access: authorized users/chats only.
Not unit tested (network + token + API key) — it runs on the operator's machine.
"""

from __future__ import annotations

import asyncio
from typing import Any

from argus.brain.agents import AnalysisContext
from argus.brain.factory import build_analyst
from argus.config.settings import Settings, get_settings
from argus.core.enums import AssetClass, Timeframe
from argus.core.schemas import DataStatus
from argus.decision.aggregator import SignalAggregator, build_aggregator, default_aggregator
from argus.interface.authorization import is_authorized
from argus.interface.feedback import feedback_keyboard, parse_feedback
from argus.interface.formatting import format_analysis_with_proposal
from argus.observability.logging import get_logger
from argus.platform.quality import QualityGate
from argus.platform.triggers import TriggerEngine
from argus.storage.base import AnalysisJournal, ProposalStore

_log = get_logger("argus.interface.bot")


def analyze_symbol_detailed(
    symbol: str,
    *,
    settings: Settings,
    timeframe: Timeframe = Timeframe.H1,
    aggregator: SignalAggregator | None = None,
    proposal_store: ProposalStore | None = None,
    journal: AnalysisJournal | None = None,
) -> tuple[str, int | None]:
    """Run the chain; return (message, journal analysis id) (synchronous).

    When ``journal`` is given the analysis is journaled, so self-monitoring also measures
    /analyze output and the id is the reference for the 👍/👎 feedback buttons.
    """
    # Symbol format: 'BASE/QUOTE' → crypto (ccxt); otherwise → BIST (borsapy).
    if "/" in symbol:
        from argus.ingestion.crypto import binance_adapter

        adapter: Any = binance_adapter()
        asset_class = AssetClass.CRYPTO
    else:
        from argus.ingestion.borsapy_provider import bist_adapter

        adapter = bist_adapter()
        asset_class = AssetClass.BIST

    series = adapter.fetch_ohlcv(symbol, timeframe, limit=100)
    report = QualityGate().check_ohlcv(series)
    if not report.ok:
        return f"⚠️ {symbol} · {timeframe.value}: data quality is insufficient (DATA PENDING).", None
    signals = TriggerEngine().evaluate(report.series)
    # When the journal is a SqliteStore, cost goes to the same ledger (shared cap).
    from argus.storage.sqlite import SqliteStore as _SqliteStore

    cost_store = journal if isinstance(journal, _SqliteStore) else None
    analysis = build_analyst(settings, cost_store=cost_store).analyze(report.series, signals)
    # L4 confluence: merge the six agent opinions (deterministic, $0). There is no news/macro
    # source here, so those agents honestly report NO_DATA; technical/risk run for real.
    ctx = AnalysisContext(
        symbol=symbol,
        timeframe=timeframe,
        series=report.series,
        signals=tuple(signals),
        asset_class=asset_class,
    )
    proposal = (aggregator or default_aggregator()).aggregate(ctx)
    if proposal_store is not None and proposal.data_status is DataStatus.OK:
        # Proposal journal (self-monitoring) — fail-safe: a write error does not fail the analysis.
        try:
            proposal_store.save_proposal(proposal, source="analyze")
        except Exception as exc:
            _log.warning("proposal-journal-error", symbol=symbol, error=repr(exc))
    analysis_id: int | None = None
    if journal is not None:
        # Analysis journal: /analyze output is measured too — fail-safe.
        try:
            analysis_id = journal.record_analysis(analysis, signals=signals)
        except Exception as exc:
            _log.warning("analysis-journal-error", symbol=symbol, error=repr(exc))
    return format_analysis_with_proposal(analysis, proposal), analysis_id


def analyze_symbol(
    symbol: str,
    *,
    settings: Settings,
    timeframe: Timeframe = Timeframe.H1,
    aggregator: SignalAggregator | None = None,
    proposal_store: ProposalStore | None = None,
) -> str:
    """Run the chain for a symbol; return the **LLM analysis + confluence** message (sync)."""
    message, _ = analyze_symbol_detailed(
        symbol,
        settings=settings,
        timeframe=timeframe,
        aggregator=aggregator,
        proposal_store=proposal_store,
    )
    return message


def build_bot(settings: Settings | None = None) -> Any:
    """Build the python-telegram-bot ``Application`` (lazy import)."""
    cfg = settings or get_settings()
    if cfg.telegram_bot_token is None:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set")
    token = cfg.telegram_bot_token.get_secret_value()
    authorized = cfg.authorized_chat_id_set
    # Build the live confluence aggregator once (including the LLM agent; do not recreate the
    # LLM client on every /analyze). In demo mode no LLM agent is added (build_aggregator
    # skips it). One store: proposal/analysis journal + 👍/👎 + cost ledger.
    from argus.interface.factory import build_store

    store = build_store(cfg)
    aggregator = build_aggregator(cfg, cost_store=store)

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler

    async def _guard(update: Any) -> bool:
        chat_id = update.effective_chat.id if update.effective_chat else None
        if chat_id is None or not is_authorized(chat_id, authorized):
            if update.message:
                await update.message.reply_text(
                    "⛔ Unauthorized. This bot is for authorized users only."
                )
            return False
        return True

    async def status(update: Any, _context: Any) -> None:
        if not await _guard(update):
            return
        from argus.interface.factory import build_status
        from argus.observability.status import format_status

        await update.message.reply_text(format_status(build_status(cfg)))

    def _to_ptb_markup(markup: dict[str, Any]) -> Any:
        """Convert the feedback_keyboard dict into a PTB InlineKeyboardMarkup."""
        rows = [
            [InlineKeyboardButton(b["text"], callback_data=b["callback_data"]) for b in row]
            for row in markup["inline_keyboard"]
        ]
        return InlineKeyboardMarkup(rows)

    async def analyze(update: Any, context: Any) -> None:
        if not await _guard(update):
            return
        if not context.args:
            await update.message.reply_text(
                "Usage: /analyze <SYMBOL>  (e.g. /analyze BTC/USDT or /analyze THYAO)"
            )
            return
        symbol = context.args[0].upper()
        await update.message.reply_text(f"⏳ Analyzing {symbol}…")
        try:
            message, analysis_id = await asyncio.to_thread(
                lambda: analyze_symbol_detailed(
                    symbol,
                    settings=cfg,
                    aggregator=aggregator,
                    proposal_store=store,
                    journal=store,
                )
            )
        except Exception as exc:  # live failure → graceful message to the user
            await update.message.reply_text(f"⚠️ Analysis failed: {exc}")
            return
        # When journaled, 👍/👎 buttons are attached (no nagging — the buttons just sit there).
        markup = None
        if analysis_id is not None and store is not None:
            markup = _to_ptb_markup(feedback_keyboard("analysis", analysis_id, symbol=symbol))
        await update.message.reply_text(message, reply_markup=markup)

    async def fb_callback(update: Any, _context: Any) -> None:
        """👍/👎 vote — authorized chats only; idempotent (last vote wins)."""
        query = update.callback_query
        if query is None:
            return
        chat = update.effective_chat
        if chat is None or not is_authorized(chat.id, authorized):
            await query.answer("⛔ Unauthorized")
            return
        vote = parse_feedback(query.data)
        if vote is None or store is None:
            await query.answer()  # not our format / no store → silent
            return
        user = update.effective_user
        user_id = str(user.id) if user is not None else str(chat.id)
        try:
            store.save_feedback(
                vote.ref_type,
                vote.ref_id,
                user_id,
                useful=vote.useful,
                symbol=vote.symbol or None,
                severity=vote.severity or None,
            )
        except Exception:  # a store error drops the vote, not the bot
            await query.answer("⚠️ Could not save")
            return
        await query.answer("Saved: " + ("👍 useful" if vote.useful else "👎 not useful"))

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("analyze", analyze))
    app.add_handler(CallbackQueryHandler(fb_callback, pattern=r"^fb\|"))
    # The 'Update' import is kept for typing/future use
    _ = Update
    return app


def run_bot(settings: Settings | None = None) -> None:
    """Run the bot with long polling."""
    build_bot(settings).run_polling()
