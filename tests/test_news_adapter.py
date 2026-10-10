"""KAPAdapter (disclosure) tests — no borsapy/pandas required (fake provider)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from argus.core.errors import AdapterError
from argus.ingestion.news import KAPAdapter

_TS = datetime(2026, 5, 29, 13, 21, 18, tzinfo=UTC)


class FakeKAPProvider:
    """Fake provider satisfying the KAPDisclosureProvider protocol."""

    def __init__(self, *, rows: list[dict[str, Any]] | None = None, raise_: bool = False) -> None:
        self._rows = rows or []
        self._raise = raise_

    def disclosures(self, symbol: str, *, limit: int) -> list[dict[str, Any]]:
        if self._raise:
            raise RuntimeError("network error")
        return self._rows[:limit]


def test_source_label() -> None:
    assert KAPAdapter(FakeKAPProvider()).source == "kap"


def test_fetch_disclosures_maps_fields() -> None:
    rows = [
        {
            "id": "1530826",
            "title": "Sample disclosure title",
            "url": "https://example.com/disclosures/1530826",
            "published_at": _TS,
        }
    ]
    out = KAPAdapter(FakeKAPProvider(rows=rows), confidence=0.99).fetch_disclosures("ACME")

    assert len(out) == 1
    d = out[0]
    assert d.disclosure_id == "1530826"
    assert d.symbol == "ACME"
    assert d.title.startswith("Sample")
    assert d.url is not None and d.url.endswith("/1530826")
    assert d.published_at == _TS
    assert d.provenance.source == "kap"
    assert d.provenance.confidence == 0.99


def test_missing_id_derives_deterministic_id() -> None:
    rows = [{"title": "Sample event", "published_at": _TS}]
    d = KAPAdapter(FakeKAPProvider(rows=rows)).fetch_disclosures("ACME")[0]
    assert d.disclosure_id == f"ACME:{_TS.isoformat()}"


def test_missing_title_raises() -> None:
    rows = [{"id": "1", "published_at": _TS}]
    with pytest.raises(AdapterError):
        KAPAdapter(FakeKAPProvider(rows=rows)).fetch_disclosures("ACME")


def test_naive_published_at_raises() -> None:
    rows = [{"id": "1", "title": "x", "published_at": datetime(2026, 1, 1)}]
    with pytest.raises(AdapterError):
        KAPAdapter(FakeKAPProvider(rows=rows)).fetch_disclosures("ACME")


def test_provider_error_wrapped() -> None:
    with pytest.raises(AdapterError):
        KAPAdapter(FakeKAPProvider(raise_=True)).fetch_disclosures("ACME")


def test_invalid_limit() -> None:
    with pytest.raises(ValueError, match="limit"):
        KAPAdapter(FakeKAPProvider()).fetch_disclosures("ACME", limit=0)


def test_limit_respected() -> None:
    rows = [{"id": str(i), "title": f"b{i}", "published_at": _TS} for i in range(5)]
    out = KAPAdapter(FakeKAPProvider(rows=rows)).fetch_disclosures("ACME", limit=3)
    assert len(out) == 3
