"""News/disclosure adapters (L1) — KAP-focused first increment.

News adapters convert news + official disclosures to the common contracts
(:class:`~argus.core.contracts.Disclosure`, :class:`~argus.core.contracts.NewsItem`).
KAP is the **required** primary source for company disclosures.

Design: isolation behind a provider (DI), as in the market-data adapters →
:class:`KAPAdapter` and its tests do not require borsapy/pandas.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from argus.core.contracts import Disclosure
from argus.core.errors import AdapterError
from argus.core.schemas import Provenance, utcnow


@runtime_checkable
class KAPDisclosureProvider(Protocol):
    """KAP disclosure provider interface.

    ``disclosures`` returns each row with these keys:
      ``id`` (str), ``title`` (str, required), ``url`` (str|None),
      ``published_at`` (tz-aware datetime).
    """

    def disclosures(self, symbol: str, *, limit: int) -> Sequence[Mapping[str, Any]]: ...


class DisclosureSource(ABC):
    """Common interface for official disclosure sources (KAP, SEC …)."""

    @property
    @abstractmethod
    def source(self) -> str: ...

    @abstractmethod
    def fetch_disclosures(self, symbol: str, *, limit: int = 50) -> list[Disclosure]: ...


class KAPAdapter(DisclosureSource):
    """Convert a KAP disclosure provider to the :class:`Disclosure` contract.

    KAP is an official/primary source, so confidence is high (default 0.99).
    """

    def __init__(
        self, provider: KAPDisclosureProvider, *, source: str = "kap", confidence: float = 0.99
    ) -> None:
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be within [0..1]")
        self._provider = provider
        self._source = source
        self._confidence = confidence

    @property
    def source(self) -> str:
        return self._source

    def fetch_disclosures(self, symbol: str, *, limit: int = 50) -> list[Disclosure]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        try:
            rows = self._provider.disclosures(symbol, limit=limit)
        except AdapterError:
            raise
        except Exception as exc:  # provider/network error → wrap as adapter error
            raise AdapterError(f"{symbol}: KAP disclosures failed: {exc}") from exc

        result: list[Disclosure] = []
        for row in rows:
            result.append(self._row_to_disclosure(row, symbol=symbol))
        return result

    def _row_to_disclosure(self, row: Mapping[str, Any], *, symbol: str) -> Disclosure:
        title = row.get("title")
        if not title:
            raise AdapterError(f"{symbol}: KAP disclosure without a title: {dict(row)!r}")

        published_at: datetime = row.get("published_at") or utcnow()
        disclosure_id = str(row.get("id") or "")
        if not disclosure_id:
            # Without an id, derive a deterministic one from (timestamp + symbol)
            disclosure_id = f"{symbol}:{published_at.isoformat()}"

        try:
            provenance = Provenance(
                source=self._source, timestamp=published_at, confidence=self._confidence
            )
            return Disclosure(
                disclosure_id=disclosure_id,
                symbol=symbol,
                title=str(title),
                url=row.get("url"),
                published_at=published_at,
                category=row.get("category"),
                provenance=provenance,
            )
        except ValidationError as exc:
            raise AdapterError(f"{symbol}: invalid KAP disclosure: {exc}") from exc
