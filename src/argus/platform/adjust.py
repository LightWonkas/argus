"""Read-time corporate-action (split/dividend) adjustment — pure functions.

Point-in-time principle: the store keeps the **raw** price ("as known at the time");
indicators and charts want the **adjusted view** for continuity. This module bridges them:

- :func:`derive_corporate_actions` — derives corporate actions from the **steps in the
  close ratio** between the raw and back-adjusted views of the same series (the borsapy
  adjust=True/False difference). Back-adjustment gives
  ``adjusted_t = raw_t × ∏(factor: action_date > t)``, so every jump in the ratio series
  is an action.
- :func:`apply_actions` — applies the actions to the raw series → adjusted view (O/H/L/C
  are multiplied; **volume is left untouched** — a volume multiplier cannot be derived
  reliably from the price ratio). The output is marked ``Provenance.adjusted=True`` (the
  view is labelled honestly).

Determinism: same input → same action list; the tolerance band keeps floating-point noise
from being mistaken for an action. With no actions the series is returned unchanged (no-op).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from argus.core.contracts import OHLCVBar, OHLCVSeries

# A ratio step below this relative threshold counts as noise (not an action).
_RATIO_TOLERANCE = 0.005  # 0.5%


@dataclass(frozen=True)
class CorporateAction:
    """A single corporate action: ``factor`` is applied to every bar before ``date``.

    Example: 1:2 split → the raw price halves after the split → ``factor = 0.5`` adjusts
    the history BEFORE the split.
    """

    symbol: str
    date: datetime  # time of the first bar the action applies to (tz-aware)
    factor: float  # multiplier applied to bars before date (>0)
    kind: str = "split"  # split | dividend | unknown (the borsapy diff cannot tell them apart)


def derive_corporate_actions(
    raw: OHLCVSeries, adjusted: OHLCVSeries, *, tolerance: float = _RATIO_TOLERANCE
) -> list[CorporateAction]:
    """Derive corporate actions from the raw + adjusted views (close-ratio steps).

    Bars are matched by timestamp (bars common to both views); if the ``adjusted/raw``
    ratio changes by more than ``tolerance`` between consecutive bars there is an action
    on that date: ``factor = ratio_before / ratio_after``. Fewer than 2 common bars →
    empty list (cannot be derived; no fabricated actions).
    """
    adj_by_ts = {b.timestamp: b for b in adjusted.bars}
    pairs: list[tuple[datetime, float]] = []  # (ts, adjusted/raw close ratio)
    for bar in raw.bars:
        other = adj_by_ts.get(bar.timestamp)
        if other is None or bar.close <= 0:
            continue
        pairs.append((bar.timestamp, float(other.close) / float(bar.close)))

    actions: list[CorporateAction] = []
    for (_, prev_ratio), (ts, ratio) in itertools.pairwise(pairs):
        if ratio <= 0 or prev_ratio <= 0:
            continue
        step = prev_ratio / ratio
        if abs(step - 1.0) > tolerance:
            actions.append(CorporateAction(symbol=raw.symbol, date=ts, factor=round(step, 8)))
    return actions


def apply_actions(series: OHLCVSeries, actions: list[CorporateAction]) -> OHLCVSeries:
    """Apply actions to the raw series → adjusted view (the store is NOT changed; new object).

    Each bar's multiplier is the product of the factors of the actions AFTER that bar
    (back-adjustment). With no actions the series is returned as is. The output carries
    ``Provenance.adjusted=True``.
    """
    relevant = sorted((a for a in actions if a.factor > 0), key=lambda a: a.date)
    if not relevant or not series.bars:
        return series

    new_bars: list[OHLCVBar] = []
    for bar in series.bars:
        multiplier = 1.0
        for action in relevant:
            if bar.timestamp < action.date:
                multiplier *= action.factor
        if multiplier == 1.0:
            new_bars.append(bar)
            continue
        m = Decimal(str(multiplier))
        new_bars.append(
            bar.model_copy(
                update={
                    "open": bar.open * m,
                    "high": bar.high * m,
                    "low": bar.low * m,
                    "close": bar.close * m,
                }
            )
        )
    return series.model_copy(
        update={
            "bars": tuple(new_bars),
            "provenance": series.provenance.model_copy(update={"adjusted": True}),
        }
    )
