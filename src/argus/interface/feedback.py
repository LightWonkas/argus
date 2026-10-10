"""Alert/analysis 👍/👎 feedback — callback data + inline keyboard (pure).

Telegram inline buttons carry ``callback_data`` (≤64 bytes). This module builds and parses
that data; it is tested without PTB or network. Votes are stored through
:class:`~argus.storage.sqlite.SqliteStore` ``save_feedback`` (idempotent — last vote wins).
**No nagging:** the buttons just sit there; votes are never requested or reminded
(alert-fatigue principle).
"""

from __future__ import annotations

from dataclasses import dataclass

# callback_data format: "fb|<ref_type>|<ref_id>|<symbol>|<severity>|<1/0>"
_PREFIX = "fb"
_SEP = "|"
_MAX_CALLBACK_BYTES = 64  # Telegram limit
_MAX_SYMBOL_LEN = 16  # symbol truncation (64-byte guarantee)


@dataclass(frozen=True)
class FeedbackVote:
    """A single parsed vote."""

    ref_type: str  # "analysis" | "alert"
    ref_id: str
    symbol: str
    severity: str
    useful: bool


def encode_feedback(
    ref_type: str, ref_id: str | int, *, symbol: str = "", severity: str = "", useful: bool
) -> str:
    """Build a vote button's callback data (never over 64 bytes; symbol cut if needed)."""
    clean_symbol = symbol.replace(_SEP, "/")[:_MAX_SYMBOL_LEN]
    data = _SEP.join(
        [_PREFIX, ref_type, str(ref_id), clean_symbol, severity, "1" if useful else "0"]
    )
    if len(data.encode("utf-8")) > _MAX_CALLBACK_BYTES:
        # Drop the symbol entirely — the id + decision are always preserved.
        data = _SEP.join([_PREFIX, ref_type, str(ref_id), "", severity, "1" if useful else "0"])
    return data


def parse_feedback(data: str | None) -> FeedbackVote | None:
    """Parse callback data; foreign or malformed data → None (not treated as a command)."""
    if not data:
        return None
    parts = data.split(_SEP)
    if len(parts) != 6 or parts[0] != _PREFIX:
        return None
    _, ref_type, ref_id, symbol, severity, flag = parts
    if not ref_type or not ref_id or flag not in ("0", "1"):
        return None
    return FeedbackVote(
        ref_type=ref_type, ref_id=ref_id, symbol=symbol, severity=severity, useful=flag == "1"
    )


def feedback_keyboard(
    ref_type: str, ref_id: str | int, *, symbol: str = "", severity: str = ""
) -> dict[str, object]:
    """👍/👎 inline keyboard (Telegram ``reply_markup`` dict — for PTB and the stdlib sender).

    One row, two buttons; it does not enlarge the message or make noise (no nagging).
    """
    return {
        "inline_keyboard": [
            [
                {
                    "text": "👍 Useful",
                    "callback_data": encode_feedback(
                        ref_type, ref_id, symbol=symbol, severity=severity, useful=True
                    ),
                },
                {
                    "text": "👎 Not useful",
                    "callback_data": encode_feedback(
                        ref_type, ref_id, symbol=symbol, severity=severity, useful=False
                    ),
                },
            ]
        ]
    }
