"""Argus entry point — ``python -m argus``.

Configures logging, then runs the selected service (watch loop, bot, dashboard, …);
with no flags it prints a banner and runs the health check.

Usage:
    python -m argus            # banner + health summary
    python -m argus --health   # health check only (OK→0, otherwise→1)
    python -m argus --version
"""

from __future__ import annotations

import argparse
import sys

from argus import __version__
from argus.config.settings import get_settings
from argus.core.errors import ConfigError
from argus.observability.health import HealthState, default_registry
from argus.observability.logging import configure_logging, get_logger


def _run_health() -> int:
    """Runs the health checks; returns the exit code (OK→0)."""
    registry = default_registry()
    log = get_logger("argus.health")
    overall = HealthState.OK
    for result in registry.run():
        log.info("health-check", check=result.name, state=result.state.value, detail=result.detail)
        if result.state is HealthState.FAIL:
            overall = HealthState.FAIL
        elif result.state is HealthState.DEGRADED and overall is not HealthState.FAIL:
            overall = HealthState.DEGRADED
    log.info("health-aggregate", state=overall.value)
    return 0 if overall is HealthState.OK else 1


def _force_utf8_stdio() -> None:
    """Forces stdout/stderr to UTF-8 (legacy Windows console code pages raise
    ``UnicodeEncodeError`` on non-ASCII/arrow characters). Harmless where UTF-8 is the default."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the exit code."""
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="argus", description="Argus — decision support (not financial advice)"
    )
    parser.add_argument("--version", action="store_true", help="Print the version and exit")
    parser.add_argument("--health", action="store_true", help="Run the health check only")
    parser.add_argument(
        "--status", action="store_true", help="Status summary (version/budget/hit rate)"
    )
    parser.add_argument("--watch", action="store_true", help="Run the watch loop daemon (24/7)")
    parser.add_argument("--bot", action="store_true", help="Run the Telegram command bot")
    parser.add_argument("--dashboard", action="store_true", help="Run the web cockpit API server")
    parser.add_argument(
        "--consumer",
        action="store_true",
        help="Redis push consumer (instant news/disclosures → Telegram)",
    )
    parser.add_argument(
        "--crypto-refresh",
        action="store_true",
        help="run the public REST universe + quality/feature/regime cycle once",
    )
    parser.add_argument(
        "--crypto-stream",
        action="store_true",
        help="run the selected public spot+perpetual WebSocket recorder (long-running)",
    )
    parser.add_argument(
        "--news-refresh",
        action="store_true",
        help="validate and store the configured public RSS/Atom news once",
    )
    args = parser.parse_args(argv)

    if args.version:
        print(f"argus {__version__}")
        return 0

    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    log = get_logger("argus")

    if args.health:
        return _run_health()

    if args.status:
        from argus.interface.factory import build_status
        from argus.observability.status import format_status

        print(format_status(build_status(settings)))
        return 0

    if args.watch:
        from argus.interface.factory import build_scheduler

        try:
            scheduler = build_scheduler()
        except (ValueError, ConfigError) as exc:
            log.error("watch-config-error", error=str(exc))
            return 1
        log.info("watch-starting", env=settings.env)
        scheduler.start()  # blocks
        return 0

    if args.bot:
        try:
            from argus.interface.bot import run_bot
        except ImportError as exc:
            log.error("telegram-dependency-error", error=str(exc))
            return 1
        log.info("telegram-bot-starting", env=settings.env)
        try:
            run_bot()  # blocks
        except (ValueError, ConfigError) as exc:
            log.error("telegram-config-error", error=str(exc))
            return 1
        return 0

    if args.consumer:
        from argus.interface.factory import build_consumer

        try:
            bus = build_consumer(settings)
        except (ValueError, ConfigError) as exc:
            log.error("consumer-config-error", error=str(exc))
            return 1
        log.info("push-consumer-starting", env=settings.env)
        bus.run()  # blocks
        return 0

    if args.crypto_refresh:
        from argus.interface.factory import build_crypto_cycle

        cycle = None
        try:
            cycle = build_crypto_cycle(settings)
            if cycle is None:
                raise ValueError("no enabled Binance REST exchange configuration")
            results = cycle.run_once()
        except (ValueError, ConfigError) as exc:
            log.error("crypto-config-error", error=str(exc))
            return 1
        finally:
            if cycle is not None:
                cycle.close()
        for result in results:
            log.info(
                "crypto-refresh",
                symbol=result.symbol,
                data_status=result.data_status.value,
                quality=result.quality_decision.value,
                records_saved=result.records_saved,
                series_saved=result.series_saved,
                candidate_id=result.candidate_id,
                errors=result.errors,
            )
        return 0 if all(result.quality_decision.allows_downstream for result in results) else 1

    if args.crypto_stream:
        import asyncio

        from argus.interface.factory import build_crypto_stream

        stream = None
        try:
            stream = build_crypto_stream(settings)
            if stream is None:
                raise ValueError("no enabled Binance WebSocket exchange configuration")
            log.info("crypto-websocket-starting", symbols="dynamic-or-bootstrap")
            asyncio.run(stream.run())
        except KeyboardInterrupt:
            log.info("crypto-websocket-stopped")
        except (ValueError, ConfigError) as exc:
            log.error("crypto-websocket-config-error", error=str(exc))
            return 1
        finally:
            if stream is not None:
                stream.close()
        return 0

    if args.news_refresh:
        from argus.interface.factory import build_news_cycle

        news_cycle = None
        try:
            news_cycle = build_news_cycle(settings)
            batch = news_cycle.run_once()
            log.info(
                "news-refresh",
                data_status=batch.data_status.value,
                records=len(batch.records),
                duplicates=batch.duplicate_count,
                provider_errors=batch.provider_errors,
            )
            return 0 if not batch.provider_errors else 1
        except (ValueError, ConfigError) as exc:
            log.error("news-config-error", error=str(exc))
            return 1
        finally:
            if news_cycle is not None:
                news_cycle.close()

    if args.dashboard:
        try:
            import uvicorn

            from argus.api.app import build_app_from_settings
        except ImportError as exc:
            log.error("dashboard-dependency-error", error=str(exc))
            return 1
        app = build_app_from_settings()
        log.info(
            "dashboard-starting",
            env=settings.env,
            bind=settings.dashboard_bind,
            port=settings.dashboard_port,
        )
        uvicorn.run(app, host=settings.dashboard_bind, port=settings.dashboard_port)  # blocks
        return 0

    log.info("argus-started", version=__version__, env=settings.env, tz=settings.timezone)
    log.info(
        "note",
        detail="Analysis and alerts only; no live orders. Outputs are not financial advice.",
    )
    return _run_health()


if __name__ == "__main__":
    sys.exit(main())
