"""Numeric conversion helpers shared across adapters.

Numbers from external sources are converted to Decimal **via str** (to avoid
float rounding errors).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from argus.core.errors import AdapterError


def to_decimal(value: object) -> Decimal:
    """Safely convert a number to Decimal; raise :class:`AdapterError` if unparsable."""
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise AdapterError(f"could not parse numeric value: {value!r}") from exc


def pos_or_none(value: object) -> Decimal | None:
    """Decimal if positive, otherwise None (for fields such as bid/ask)."""
    if value is None:
        return None
    d = to_decimal(value)
    return d if d > 0 else None


def nonneg_or_none(value: object) -> Decimal | None:
    """Decimal if non-negative, otherwise None (for fields such as volume)."""
    if value is None:
        return None
    d = to_decimal(value)
    return d if d >= 0 else None
