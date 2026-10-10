#!/usr/bin/env python
"""News poller — the push PRODUCER: scan sources -> archive -> label -> notify.

Chain (every round):
  1. Sources (CryptoPanic/Finnhub — those with a token; KAP is handled by feed_once) ->
     ``save_news`` (``(source, ext_id)`` dedup -> idempotent; a restart creates no duplicates).
  2. Fear & Greed (alternative.me, no token) -> macro store (PositioningAgent input).
  3. Labelling: ``unrouted_news`` -> ``NewsRouter.route()`` -> label stored in the archive
     (budget/error -> ``status=budget_exceeded|error``, label NULL; demo router -> skipped).
  4. Notification: ``relevant ∧ importance ≥ high`` -> through the AlertDispatcher path
     (prioritization + quiet hours; NO direct channel is opened — alert-fatigue principle).
  5. (Optional) Redis ``news.received`` publish — silently skipped without Redis (the archive
     still fills; setups with a push consumer receive the event).

Checkpoint: the last ext_id per source is persisted in the ``checkpoint`` table; the actual
idempotency comes from DB dedup + ``route_status`` (safe across crashes/restarts).

Run:
    python scripts/news_poller.py                # single round
    python scripts/news_poller.py --loop 300     # every 5 minutes (suggested)
"""

from __future__ import annotations

import argparse
import sys
import time

if sys.platform == "win32":  # pragma: no cover — legacy console code page
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")

from argus.config.settings import Settings, get_settings
from argus.core.errors import BudgetExceededError
from argus.core.schemas import utcnow
from argus.interface.factory import _SQLITE_PREFIX, to_alert_policy
from argus.storage.base import NewsRecord
from argus.storage.sqlite import SqliteStore

# Notification threshold: relevant ∧ importance ≥ high.
_NOTIFY_MIN = {"high", "critical"}


def _build_store(cfg: Settings) -> SqliteStore:
    url = cfg.database_url
    if not url.startswith(_SQLITE_PREFIX):
        raise SystemExit(f"database_url is not SQLite ({url}).")
    return SqliteStore(url[len(_SQLITE_PREFIX) :] or ":memory:")


def poll_sources(cfg: Settings, store: SqliteStore, *, verbose: bool = True) -> int:
    """Scan the sources that have a token, archive the items; return the new-record count."""
    from argus.ingestion.news_api import CryptoPanicSource, FinnhubSource

    sources: list[object] = []
    if cfg.cryptopanic_token is not None:
        sources.append(CryptoPanicSource(cfg.cryptopanic_token.get_secret_value()))
    if cfg.finnhub_key is not None:
        sources.append(FinnhubSource(cfg.finnhub_key.get_secret_value()))
    if not sources and verbose:
        print(
            "  ⊘ SKIP — no news token (ARGUS_CRYPTOPANIC_TOKEN / ARGUS_FINNHUB_KEY); "
            "only F&G + labelling run"
        )

    total_new = 0
    for source in sources:
        name = source.source  # type: ignore[attr-defined]
        try:
            items = source.fetch(limit=50)  # type: ignore[attr-defined]
        except Exception as exc:  # rate limit/network -> skipped this round, and labelled so
            if verbose:
                print(f"  ⊘ {name:14} SKIP — {type(exc).__name__} (retried next round)")
            continue
        added = store.save_news(items)
        total_new += added
        if items:
            store.set_checkpoint(f"news:{name}:last_ext_id", items[0].id)
        if verbose:
            print(f"  ✓ {name:14} {len(items):>3} news items fetched · {added} newly archived")
    return total_new


def poll_fear_greed(store: SqliteStore, *, verbose: bool = True) -> None:
    """Write the F&G index to the macro store (PositioningAgent input); error -> SKIP."""
    from argus.ingestion.news_api import FearGreedSource

    try:
        value, trend = FearGreedSource().fetch()
    except Exception as exc:
        if verbose:
            print(f"  ⊘ fear&greed SKIP — {type(exc).__name__}")
        return
    # The value is always written (PositioningAgent reads the LEVEL); no trend -> 0 (flat).
    store.save_macro({"crypto_fng": value}, {"crypto_fng": trend} if trend else {})
    if verbose:
        print(f"  ✓ fear&greed    {value:.0f} (trend {trend if trend is not None else 'n/a'})")


def route_pending(
    cfg: Settings, store: SqliteStore, *, limit: int = 50, verbose: bool = True
) -> tuple[int, int]:
    """Run unlabelled news through the router; return (labelled, notified)."""
    from argus.brain.news_router import build_news_router

    router = build_news_router(cfg, cost_store=store)  # cost goes to the shared ledger
    if router.demo:
        if verbose:
            print("  ⊘ router is in demo mode (no real LLM) — labelling skipped")
        return 0, 0

    dispatcher = _build_dispatcher(cfg)
    pending = store.unrouted_news(limit=limit)
    routed = notified = 0
    for record in pending:
        try:
            label = router.route(
                record.title,
                source=record.source,
                symbols=[record.symbol] if record.symbol else [],
            )
        except BudgetExceededError:
            store.update_news_route(record.source, record.ext_id, status="budget_exceeded")
            if verbose:
                print("  ⊘ budget exhausted — remaining news items wait for the next round")
            break  # budget exhausted: stop the round (the rest keep route_status NULL -> retried)
        except Exception:
            store.update_news_route(record.source, record.ext_id, status="error")
            continue
        store.update_news_route(
            record.source,
            record.ext_id,
            status="ok",
            importance=label.importance.value,
            relevant=label.relevant,
            target_agent=label.target_agent,
            confidence_pct=label.confidence_pct,
            model=label.model,
        )
        routed += 1
        if label.relevant and label.importance.value in _NOTIFY_MIN and dispatcher is not None:
            dispatcher.dispatch_message(_format_news_alert(record, label), label.importance)
            notified += 1
    if verbose:
        print(f"  ✓ {routed} news items labelled · {notified} notifications dispatched")
    return routed, notified


def _build_dispatcher(cfg: Settings):
    """Policy-routed dispatcher when Telegram is configured; otherwise None (no notifications)."""
    if cfg.telegram_bot_token is None or not cfg.authorized_chat_id_set:
        return None
    from argus.config.loader import ConfigLoader
    from argus.interface.dispatcher import AlertDispatcher
    from argus.interface.notifier import TelegramNotifier
    from argus.interface.telegram_sender import TelegramHttpSender

    runtime = ConfigLoader().get()
    sender = TelegramHttpSender(cfg.telegram_bot_token.get_secret_value())
    notifier = TelegramNotifier(sender, cfg.authorized_chat_id_set)
    return AlertDispatcher(notifier, policy=to_alert_policy(runtime.alerts))


def _format_news_alert(record: NewsRecord, label) -> str:
    symbol = f" [{record.symbol}]" if record.symbol else ""
    return (
        f"📰 {label.importance.value.upper()}{symbol} — {record.title}\n"
        f"Target: {label.target_agent} · confidence {label.confidence_pct}%\n"
        f"{record.url or ''}\n"
        f"(news labels are informational, not financial advice)"
    )


def publish_events(cfg: Settings, new_count: int, *, verbose: bool = True) -> None:
    """Publish news.received to Redis when there is new news; silently skipped without Redis."""
    if new_count <= 0:
        return
    try:
        from argus.core.events import Event, EventType
        from argus.platform.event_bus import RedisEventBus

        bus = RedisEventBus(cfg.redis_url)
        bus.publish(
            Event(
                type=EventType.NEWS_RECEIVED,
                source="news_poller",
                payload={"count": new_count},
            )
        )
        if verbose:
            print(f"  ✓ Redis news.received published (count={new_count})")
    except Exception as exc:  # Redis not installed/running -> the archive still filled (degraded)
        if verbose:
            print(f"  ⊘ Redis publish skipped ({type(exc).__name__}) — the archive is up to date")


def run_once(*, verbose: bool = True) -> None:
    cfg = get_settings()
    store = _build_store(cfg)
    try:
        print("── Source scan ──")
        new_count = poll_sources(cfg, store, verbose=verbose)
        print("── Fear & Greed ──")
        poll_fear_greed(store, verbose=verbose)
        print("── Labelling (NewsRouter) ──")
        route_pending(cfg, store, verbose=verbose)
        print("── Event publish ──")
        publish_events(cfg, new_count, verbose=verbose)
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="news_poller", description="Argus news poller")
    parser.add_argument(
        "--loop",
        type=int,
        default=0,
        metavar="SEC",
        help="Repeat at this interval in seconds (0 = single round)",
    )
    args = parser.parse_args(argv)
    print(f"{'═' * 60}\nArgus news poller — {utcnow():%Y-%m-%d %H:%M:%S} UTC\n{'═' * 60}")
    while True:
        run_once()
        if args.loop <= 0:
            return 0
        print(f"\n… waiting {args.loop} s (Ctrl+C to quit) …\n")
        try:
            time.sleep(args.loop)
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0


if __name__ == "__main__":
    sys.exit(main())
