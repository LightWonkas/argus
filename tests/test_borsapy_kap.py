"""Tests for the pure borsapy KAP helpers (no borsapy/pandas required)."""

from __future__ import annotations

import pytest

from argus.core.errors import AdapterError
from argus.ingestion.borsapy_kap import extract_disclosure_id, parse_kap_datetime


def test_parse_kap_datetime_istanbul_to_utc() -> None:
    # 19:21:18 Istanbul (UTC+3) → 16:21:18 UTC
    out = parse_kap_datetime("29.12.2025 19:21:18")
    assert out.utcoffset() is not None and out.utcoffset().total_seconds() == 0
    assert (out.hour, out.minute, out.second) == (16, 21, 18)
    assert (out.year, out.month, out.day) == (2025, 12, 29)


def test_parse_kap_datetime_date_only() -> None:
    out = parse_kap_datetime("29.12.2025")
    assert out.tzinfo is not None  # 00:00 Istanbul → 21:00 UTC (28.12)
    assert out.utcoffset() is not None and out.utcoffset().total_seconds() == 0


def test_parse_kap_datetime_bad_raises() -> None:
    with pytest.raises(AdapterError):
        parse_kap_datetime("not-a-date")


def test_extract_disclosure_id() -> None:
    assert extract_disclosure_id("https://www.kap.org.tr/tr/Bildirim/1530826") == "1530826"


def test_extract_disclosure_id_no_match() -> None:
    assert extract_disclosure_id("https://example.com/x") == ""
    assert extract_disclosure_id("") == ""
