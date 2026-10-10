"""L6 — Interface/orchestration layer.

Telegram bot (instant alerts, /analyze /status, authorization) · Scheduler (watch loop) ·
Internal API. Output language is English.

Includes message formatting, authorization, TelegramNotifier (delivery retry),
TelegramHttpSender (live push), the command bot (PTB) and the watch scheduler.
"""

from argus.interface.authorization import is_authorized
from argus.interface.briefing import analysis_brief_line, format_briefing
from argus.interface.cooldown import CooldownTracker
from argus.interface.crypto_runtime import (
    CryptoCycleResult,
    CryptoSurveillanceCycle,
    CryptoWebSocketService,
)
from argus.interface.dispatcher import AlertDispatcher
from argus.interface.factory import (
    build_crypto_cycle,
    build_crypto_stream,
    build_scheduler,
    build_watch_loop,
)
from argus.interface.formatting import (
    format_alert,
    format_analysis,
    format_analysis_with_proposal,
    format_proposal,
)
from argus.interface.notifier import DeliveryResult, MessageSender, TelegramNotifier
from argus.interface.prioritization import AlertPolicy, AlertRoute, alert_severity
from argus.interface.quiet_hours import QuietHours
from argus.interface.scheduler import TierJob, WatchScheduler, tier_jobs
from argus.interface.sessions import is_market_open, scan_interval_seconds
from argus.interface.telegram_sender import TelegramHttpSender
from argus.interface.watch_loop import ScanResult, ScanStatus, WatchLoop

__all__ = [
    "AlertDispatcher",
    "AlertPolicy",
    "AlertRoute",
    "CooldownTracker",
    "CryptoCycleResult",
    "CryptoSurveillanceCycle",
    "CryptoWebSocketService",
    "DeliveryResult",
    "MessageSender",
    "QuietHours",
    "ScanResult",
    "ScanStatus",
    "TelegramHttpSender",
    "TelegramNotifier",
    "TierJob",
    "WatchLoop",
    "WatchScheduler",
    "alert_severity",
    "analysis_brief_line",
    "build_crypto_cycle",
    "build_crypto_stream",
    "build_scheduler",
    "build_watch_loop",
    "format_alert",
    "format_analysis",
    "format_analysis_with_proposal",
    "format_briefing",
    "format_proposal",
    "is_authorized",
    "is_market_open",
    "scan_interval_seconds",
    "tier_jobs",
]
