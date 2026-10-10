"""borsapy-based KAP disclosure provider (live).

``borsapy`` is an unofficial third-party library. This is an optional integration,
disabled unless the ``bist`` extra is installed; users are responsible for
complying with the upstream data sources' terms of service.

borsapy + pandas are lazily imported at runtime only. The pure helpers
(``parse_kap_datetime``, ``extract_disclosure_id``) are unit tested; the pandas/network
glue (``disclosures``) is smoke tested on the user's machine.

Verified borsapy API: ``Ticker(sym).news`` → DataFrame with columns
``Date`` ("29.12.2025 19:21:18", Istanbul time), ``Title``, ``URL``
(``https://www.kap.org.tr/tr/Bildirim/<id>``).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from argus.core.errors import AdapterError

if TYPE_CHECKING:
    from argus.ingestion.news import KAPAdapter

_ISTANBUL = ZoneInfo("Europe/Istanbul")
_ID_RE = re.compile(r"/Bildirim/(\d+)")
_KAP_FORMATS = ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y")


def parse_kap_datetime(value: str) -> datetime:
    """Convert a KAP date string (Istanbul time) to tz-aware UTC."""
    text = str(value).strip()
    for fmt in _KAP_FORMATS:
        try:
            naive = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return naive.replace(tzinfo=_ISTANBUL).astimezone(UTC)
    raise AdapterError(f"could not parse KAP date: {value!r}")


def extract_disclosure_id(url: str) -> str:
    """Extract the disclosure number from a KAP disclosure URL ('' = not found)."""
    match = _ID_RE.search(url or "")
    return match.group(1) if match else ""


def _column(df: Any, name: str) -> list[Any]:
    for candidate in (name, name.lower()):
        if candidate in df.columns:
            values: list[Any] = df[candidate].tolist()
            return values
    raise AdapterError(f"KAP column not found: {name}")


class BorsapyKAPProvider:
    """Live KAP disclosure provider (borsapy). Implements ``KAPDisclosureProvider``."""

    def __init__(self) -> None:
        import borsapy  # lazy import

        self._bp = borsapy

    def disclosures(self, symbol: str, *, limit: int) -> list[dict[str, Any]]:
        df = self._bp.Ticker(symbol).news  # property → DataFrame
        dates = _column(df, "Date")
        titles = _column(df, "Title")
        urls = _column(df, "URL")

        rows: list[dict[str, Any]] = []
        for i in range(len(titles)):
            try:
                published_at = parse_kap_datetime(str(dates[i]))
            except AdapterError:
                continue  # skip rows with an unparsable date (honesty: nothing is made up)
            url = str(urls[i]) if urls[i] is not None else None
            rows.append(
                {
                    "id": extract_disclosure_id(url or ""),
                    "title": str(titles[i]),
                    "url": url,
                    "published_at": published_at,
                }
            )
        return rows[:limit]  # .news is ordered newest to oldest


def kap_provider() -> BorsapyKAPProvider:
    """Build a live :class:`BorsapyKAPProvider` (borsapy is lazily imported)."""
    return BorsapyKAPProvider()


def kap_adapter(*, confidence: float = 0.99) -> KAPAdapter:
    """Build a ``KAPAdapter`` backed by the live borsapy KAP provider."""
    from argus.ingestion.news import KAPAdapter

    return KAPAdapter(BorsapyKAPProvider(), source="kap", confidence=confidence)
