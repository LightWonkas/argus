"""TelegramHttpSender — plain HTTPS POST to the Telegram Bot API (stdlib, no extra deps).

Sending a message is a single ``sendMessage`` POST; PTB is not needed for it. The token is
a **secret** and is never logged. The pure helpers (URL/payload) are unit tested; the
network call runs on the operator's machine.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import urlencode

from argus.core.errors import ArgusError

_API_BASE = "https://api.telegram.org"


def api_url(token: str, method: str = "sendMessage") -> str:
    """Bot API method URL (the token is a secret → never logged)."""
    return f"{_API_BASE}/bot{token}/{method}"


def build_payload(
    chat_id: int, text: str, *, reply_markup: dict[str, object] | None = None
) -> dict[str, str]:
    """sendMessage body (``reply_markup`` is sent to Telegram as a JSON string)."""
    payload = {"chat_id": str(chat_id), "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    return payload


class TelegramHttpSender:
    """``MessageSender`` — sends messages through the Telegram Bot API."""

    def __init__(self, token: str, *, timeout: float = 10.0) -> None:
        if not token:
            raise ValueError("Telegram bot token must not be empty")
        self._token = token
        self._timeout = timeout

    def send_message(self, chat_id: int, text: str) -> None:
        self._post(chat_id, text)

    def send_message_markup(
        self, chat_id: int, text: str, *, reply_markup: dict[str, object]
    ) -> None:
        """Message with an inline keyboard (👍/👎). Same sendMessage plus reply_markup."""
        self._post(chat_id, text, reply_markup=reply_markup)

    def _post(
        self, chat_id: int, text: str, *, reply_markup: dict[str, object] | None = None
    ) -> None:
        data = urlencode(build_payload(chat_id, text, reply_markup=reply_markup)).encode("utf-8")
        request = urllib.request.Request(api_url(self._token), data=data, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ArgusError(f"Telegram delivery failed (chat {chat_id}): {exc}") from exc
        if not body.get("ok", False):
            raise ArgusError(f"Telegram API error: {body.get('description', body)}")
