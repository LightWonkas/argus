"""Common interface for L1 adapters.

Adapter pattern: every source is converted to the common contract; upper layers see
this interface rather than the source → swapping a source means swapping a single
adapter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from argus.core.contracts import OHLCVSeries, Quote
from argus.core.enums import Timeframe


class MarketDataAdapter(ABC):
    """Contract implemented by market-data adapters (crypto/TR/global)."""

    @property
    @abstractmethod
    def source(self) -> str:
        """Provenance label, e.g. ``'ccxt:binance'`` (written to every record)."""

    @abstractmethod
    def fetch_quote(self, symbol: str) -> Quote:
        """Return the current quote for a single instrument."""

    @abstractmethod
    def fetch_ohlcv(self, symbol: str, timeframe: Timeframe, *, limit: int = 100) -> OHLCVSeries:
        """Return the last ``limit`` candles for an instrument + timeframe."""
