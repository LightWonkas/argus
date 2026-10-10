"""Session & calendar awareness — does not poll a closed market for nothing.

Approximate session hours (UTC internally): crypto 24/7; metals/forex 24/5; BIST
10:00–18:00 Istanbul time (UTC 07:00–15:00) Mon–Fri; US equities 16:30–23:00 Istanbul time
(UTC 13:30–20:00) Mon–Fri. There is no holiday calendar; weekdays are an approximation.
"""

from __future__ import annotations

from datetime import datetime, time

from argus.config.models import WatchTier
from argus.core.enums import AssetClass

# (UTC open, UTC close) — weekday session windows.
_SESSIONS: dict[AssetClass, tuple[time, time]] = {
    AssetClass.BIST: (time(7, 0), time(15, 0)),  # Istanbul 10:00–18:00
    AssetClass.EQUITY: (time(13, 30), time(20, 0)),  # Istanbul 16:30–23:00
}

# Tier → scan interval (seconds). T0 frequent, T2 light.
_TIER_INTERVAL_S: dict[WatchTier, int] = {
    "holdings": 300,  # 5 min
    "watchlist": 900,  # 15 min
    "broad": 3600,  # hourly
}


def is_market_open(asset_class: AssetClass, now: datetime) -> bool:
    """Return whether the market is open at ``now`` (tz-aware UTC); approximate."""
    if now.tzinfo is None:
        raise ValueError("now must be tz-aware (UTC)")
    if asset_class is AssetClass.CRYPTO:
        return True  # 24/7
    is_weekday = now.weekday() < 5  # Mon=0 … Fri=4
    if asset_class in (AssetClass.METAL, AssetClass.FX):
        return is_weekday  # 24/5 (approximate)
    window = _SESSIONS.get(asset_class)
    if window is None:
        return is_weekday
    open_t, close_t = window
    return is_weekday and open_t <= now.timetz().replace(tzinfo=None) <= close_t


def scan_interval_seconds(tier: WatchTier) -> int:
    """Scan interval for the tier (seconds)."""
    return _TIER_INTERVAL_S[tier]
