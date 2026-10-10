"""TelegramHttpSender pure helper tests (no network required)."""

from __future__ import annotations

import pytest

from argus.interface import TelegramHttpSender
from argus.interface.telegram_sender import api_url, build_payload


def test_api_url_includes_token_and_method() -> None:
    url = api_url("123:ABC", "sendMessage")
    assert url == "https://api.telegram.org/bot123:ABC/sendMessage"


def test_build_payload() -> None:
    payload = build_payload(42, "hello")
    assert payload == {"chat_id": "42", "text": "hello"}


def test_empty_token_rejected() -> None:
    with pytest.raises(ValueError, match="token"):
        TelegramHttpSender("")
