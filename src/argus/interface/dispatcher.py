"""AlertDispatcher — wires prioritization + quiet hours + digest into notification.

A smart dispatch layer the WatchLoop can use instead of calling
``notifier.send_analysis`` directly: depending on the severity/quiet-hours policy it sends
instantly, defers (digest) or suppresses. ``flush_digest`` publishes the daily briefing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from argus.core.contracts import AnalysisOutput
from argus.core.enums import Severity
from argus.core.schemas import utcnow
from argus.interface.briefing import analysis_brief_line, format_briefing
from argus.interface.notifier import DeliveryResult, TelegramNotifier
from argus.interface.prioritization import AlertPolicy, AlertRoute


@dataclass(frozen=True)
class _DigestItem:
    line: str
    detail: str | None = None
    reply_markup: dict[str, object] | None = None


class AlertDispatcher:
    """Dispatches alerts as instant/digest/suppressed according to the policy."""

    def __init__(
        self,
        notifier: TelegramNotifier,
        *,
        policy: AlertPolicy | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._notifier = notifier
        self._policy = policy or AlertPolicy()
        self._clock = clock
        self._digest: list[_DigestItem] = []

    @property
    def pending_digest(self) -> tuple[str, ...]:
        return tuple(item.line for item in self._digest)

    def dispatch_analysis(
        self,
        analysis: AnalysisOutput,
        severity: Severity,
        *,
        now: datetime | None = None,
        text: str | None = None,
        reply_markup: dict[str, object] | None = None,
    ) -> AlertRoute:
        """Dispatch an analysis per the severity + quiet-hours policy; return the route."""
        reference = now or self._clock()
        route = self._policy.route(severity, reference)
        if route is AlertRoute.INSTANT:
            if text is None and reply_markup is None:
                self._notifier.send_analysis(analysis)
            else:
                self._notifier.broadcast(
                    text or analysis_brief_line(analysis), reply_markup=reply_markup
                )
        elif route is AlertRoute.DIGEST:
            self._digest.append(
                _DigestItem(
                    line=analysis_brief_line(analysis),
                    detail=text,
                    reply_markup=reply_markup,
                )
            )
        # SUPPRESSED → nothing
        return route

    def dispatch_message(
        self,
        text: str,
        severity: Severity,
        *,
        now: datetime | None = None,
        reply_markup: dict[str, object] | None = None,
    ) -> AlertRoute:
        """Dispatch free text (e.g. tagged news) through the same policy path.

        Alert-fatigue principle: news notifications also go through prioritization +
        quiet hours + digest; NO direct channel is opened.
        """
        reference = now or self._clock()
        route = self._policy.route(severity, reference)
        if route is AlertRoute.INSTANT:
            self._notifier.broadcast(text, reply_markup=reply_markup)
        elif route is AlertRoute.DIGEST:
            self._digest.append(_DigestItem(line=text, detail=text, reply_markup=reply_markup))
        # SUPPRESSED → nothing
        return route

    def flush_digest(
        self, *, now: datetime | None = None, title: str = "Daily briefing"
    ) -> list[DeliveryResult]:
        """Publish the accumulated digest as a single briefing message and clear it."""
        if not self._digest:
            return []
        items = list(self._digest)
        message = format_briefing(now or self._clock(), [item.line for item in items], title=title)
        results = self._notifier.broadcast(message)
        self._digest.clear()
        # Keep the detailed proposal + vote buttons on the digest path too. Older callers
        # use ``detail=None``, so the single-briefing behaviour stays backward compatible.
        for item in items:
            if item.detail is not None:
                results.extend(
                    self._notifier.broadcast(item.detail, reply_markup=item.reply_markup)
                )
        return results
