"""TelegramNotifier — broadcast to authorized chats with delivery retries.

The sender (:class:`MessageSender`) is injected, so unit tests need no network/PTB.
A failed alert delivery is retried. Messages go to authorized chats only.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from argus.core.contracts import AnalysisOutput
from argus.core.enums import Timeframe
from argus.interface.formatting import format_alert, format_analysis
from argus.observability.logging import get_logger
from argus.platform.triggers import TriggerSignal

_log = get_logger("argus.interface.notifier")


@runtime_checkable
class MessageSender(Protocol):
    """Interface that sends a text message to a single chat (PTB/HTTP/fake)."""

    def send_message(self, chat_id: int, text: str) -> None: ...


@runtime_checkable
class MarkupSender(Protocol):
    """Sender with inline-keyboard support (optional capability — 👍/👎).

    If the sender lacks it the notifier falls back to plain text (buttons are lost, the
    message is not).
    """

    def send_message_markup(
        self, chat_id: int, text: str, *, reply_markup: dict[str, object]
    ) -> None: ...


@dataclass(frozen=True)
class DeliveryResult:
    """Delivery result for a single chat."""

    chat_id: int
    ok: bool
    attempts: int
    error: str | None = None


class TelegramNotifier:
    """Broadcasts messages to authorized chats; retries failed deliveries."""

    def __init__(
        self, sender: MessageSender, authorized_chat_ids: Iterable[int], *, max_retries: int = 2
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        self._sender = sender
        self._chats = tuple(dict.fromkeys(authorized_chat_ids))  # unique, ordered
        self._max_retries = max_retries

    @property
    def chat_ids(self) -> tuple[int, ...]:
        return self._chats

    def broadcast(
        self, text: str, *, reply_markup: dict[str, object] | None = None
    ) -> list[DeliveryResult]:
        """Send the text to every authorized chat, retrying per chat.

        When ``reply_markup`` is given and the sender supports it, an inline keyboard is
        attached (👍/👎); otherwise plain text is sent (the message is never dropped).
        """
        results: list[DeliveryResult] = []
        for chat_id in self._chats:
            results.append(self._deliver(chat_id, text, reply_markup=reply_markup))
        return results

    def _deliver(
        self, chat_id: int, text: str, *, reply_markup: dict[str, object] | None = None
    ) -> DeliveryResult:
        last_error: str | None = None
        for attempt in range(1, self._max_retries + 2):  # first attempt + retries
            try:
                if reply_markup is not None and isinstance(self._sender, MarkupSender):
                    self._sender.send_message_markup(chat_id, text, reply_markup=reply_markup)
                else:
                    self._sender.send_message(chat_id, text)
                return DeliveryResult(chat_id=chat_id, ok=True, attempts=attempt)
            except Exception as exc:  # delivery failure → retry
                last_error = repr(exc)
                _log.warning(
                    "telegram-delivery-error", chat_id=chat_id, attempt=attempt, error=last_error
                )
        return DeliveryResult(
            chat_id=chat_id, ok=False, attempts=self._max_retries + 1, error=last_error
        )

    def send_analysis(self, analysis: AnalysisOutput) -> list[DeliveryResult]:
        return self.broadcast(format_analysis(analysis))

    def send_alert(
        self, symbol: str, timeframe: Timeframe, signals: list[TriggerSignal]
    ) -> list[DeliveryResult]:
        return self.broadcast(format_alert(symbol, timeframe, signals))
