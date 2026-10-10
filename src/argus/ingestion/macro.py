"""MacroAdapter — TCMB EVDS macro series (L1 adapter pattern).

borsapy's EVDS wrapper (``set_evds_key`` + ``evds_series``) is hidden behind a **provider**
(:class:`EVDSProvider`) → unit tests need no network/borsapy (DI). The pure part derives a
**trend direction** from the observation list (MacroIntermarketAgent reads the values as
trends: positive=rising, negative=falling; magnitude is strength).

Honesty: without a key the adapter cannot be built (the caller SKIPs, no fake macro data);
if a series is empty or has a single observation no trend is derived (that metric is
skipped); ``real_rate`` is computed only when both policy_rate and cpi_yoy are present
(real rate = nominal − inflation; computed tier [A] data).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from argus.core.errors import AdapterError
from argus.core.schemas import Provenance, utcnow

if TYPE_CHECKING:
    from collections.abc import Mapping


@runtime_checkable
class EVDSProvider(Protocol):
    """The EVDS surface MacroAdapter needs (a fake is passed in tests)."""

    def series(self, code: str, *, lookback: int) -> list[tuple[datetime, float]]:
        """The last ``lookback`` observations of a series (oldest→newest, tz-aware)."""
        ...


@dataclass(frozen=True)
class MacroSnapshot:
    """Result of one macro fetch: latest values + trend directions + provenance."""

    values: dict[str, float]  # metric → latest observation value
    trends: dict[
        str, float
    ]  # metric → trend direction (+1 rising / -1 falling; 0 is never emitted)
    provenance: Provenance


def derive_trend(observations: list[tuple[datetime, float]]) -> float | None:
    """Trend direction from observations (pure): +1 if the last value rose, -1 if it fell.

    ``None`` with <2 observations or no change — no direction is made up (the metric is
    left out of the snapshot).
    """
    if len(observations) < 2:
        return None
    prev, last = observations[-2][1], observations[-1][1]
    if last > prev:
        return 1.0
    if last < prev:
        return -1.0
    return None


class MacroAdapter:
    """Convert EVDS series to a :class:`MacroSnapshot` (using the configured series map)."""

    def __init__(self, provider: EVDSProvider, *, source: str = "evds") -> None:
        self._provider = provider
        self._source = source

    def fetch(self, series_map: Mapping[str, str], *, lookback: int = 12) -> MacroSnapshot:
        """Fetch every mapped series into value+trend; one failing series does not stop the rest.

        Raises :class:`AdapterError` if no series could be fetched (no silent empty snapshot).
        """
        values: dict[str, float] = {}
        trends: dict[str, float] = {}
        last_ts: datetime | None = None
        errors: list[str] = []
        for key, code in series_map.items():
            try:
                observations = self._provider.series(code, lookback=lookback)
            except Exception as exc:  # one failing series does not take the rest down (fail-safe)
                errors.append(f"{key}({code}): {exc}")
                continue
            if not observations:
                continue
            values[key] = observations[-1][1]
            trend = derive_trend(observations)
            if trend is not None:
                trends[key] = trend
            ts = observations[-1][0]
            if last_ts is None or ts > last_ts:
                last_ts = ts

        # Real rate = policy rate − CPI YoY (computed [A]; the agent knows 'real_rate').
        if "policy_rate" in values and "cpi_yoy" in values:
            values["real_rate"] = values["policy_rate"] - values["cpi_yoy"]
            if "policy_rate" in trends or "cpi_yoy" in trends:
                # Real-rate trend: sign of nominal trend − inflation trend (crude but
                # honest: the composite if either has a direction; none if they cancel).
                composite = trends.get("policy_rate", 0.0) - trends.get("cpi_yoy", 0.0)
                if composite:
                    trends["real_rate"] = 1.0 if composite > 0 else -1.0

        if not values:
            raise AdapterError(
                "EVDS macro fetch failed: "
                + ("; ".join(errors) if errors else "all series are empty")
            )
        return MacroSnapshot(
            values=values,
            trends=trends,
            provenance=Provenance(
                source=self._source, timestamp=last_ts or utcnow(), confidence=0.9
            ),
        )


class BorsapyEVDSProvider:
    """Live EVDS provider — borsapy is lazily imported (key required)."""

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("EVDS key must not be empty")
        import borsapy  # lazy: only needed for live use

        self._bp = borsapy
        self._bp.set_evds_key(api_key)

    def series(self, code: str, *, lookback: int) -> list[tuple[datetime, float]]:
        df = self._bp.evds_series(code)
        rows: list[tuple[datetime, float]] = []
        for ts, value in zip(df.index, df.iloc[:, 0].tolist(), strict=False):
            try:
                v = float(value)
            except (TypeError, ValueError):
                continue
            if v != v:  # NaN
                continue
            dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
            if isinstance(dt, datetime):
                if dt.tzinfo is None:
                    from zoneinfo import ZoneInfo

                    dt = dt.replace(tzinfo=ZoneInfo("Europe/Istanbul"))
                rows.append((dt, v))
        return rows[-lookback:]


def evds_adapter(api_key: str) -> MacroAdapter:
    """Live EVDS macro adapter (borsapy is lazily imported)."""
    return MacroAdapter(BorsapyEVDSProvider(api_key))
