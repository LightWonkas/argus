"""Crypto triggers and the multi-source candidate qualification gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from argus.core.crypto import FeatureSnapshot, MarketRegime
from argus.core.enums import MarketRegimeKind, Severity, Stance, Timeframe
from argus.platform.quality import CryptoQualityReport, QualityDecision


class CryptoTriggerGroup(StrEnum):
    TECHNICAL = "technical"
    VOLATILITY = "volatility"
    ORDER_FLOW = "volume_orderflow"
    FUNDING_OI = "funding_oi"
    LIQUIDATION = "liquidation"
    REGIME = "regime_change"
    NEWS_EVENT = "news_event"
    DATA_OPERATIONAL = "data_operational_risk"


class CryptoTrigger(BaseModel):
    """Directional or non-directional crypto trigger with a validity window and evidence ids."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    group: CryptoTriggerGroup
    direction: Stance | None = None
    severity: Severity
    horizon: Timeframe
    evidence_sources: tuple[str, ...] = Field(..., min_length=1)
    valid_until: datetime
    invalidation: str = Field(..., min_length=1)
    data_quality: QualityDecision
    message: str = Field(..., min_length=1)

    @field_validator("valid_until")
    @classmethod
    def _valid_until_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("valid_until must be a tz-aware datetime")
        return value.astimezone(UTC)


class CryptoTriggerEngine:
    """Emit deterministic, LLM-free triggers from the feature/regime context."""

    def evaluate(
        self,
        features: FeatureSnapshot,
        regime: MarketRegime,
        quality: CryptoQualityReport,
        *,
        previous_regime: MarketRegimeKind | None = None,
        news_event: tuple[Stance | None, Severity, str] | None = None,
    ) -> list[CryptoTrigger]:
        values = features.values
        valid_until = features.as_of + timedelta(seconds=features.timeframe.seconds)
        triggers: list[CryptoTrigger] = []

        def evidence(group: str, fallback: str) -> tuple[str, ...]:
            return features.evidence_sources.get(group, (fallback,))

        def add(
            group: CryptoTriggerGroup,
            direction: Stance | None,
            severity: Severity,
            sources: tuple[str, ...],
            message: str,
            invalidation: str,
        ) -> None:
            triggers.append(
                CryptoTrigger(
                    symbol=features.symbol,
                    group=group,
                    direction=direction,
                    severity=severity,
                    horizon=features.timeframe,
                    evidence_sources=sources,
                    valid_until=valid_until,
                    invalidation=invalidation,
                    data_quality=quality.decision,
                    message=message,
                )
            )

        if not quality.allows_downstream:
            add(
                CryptoTriggerGroup.DATA_OPERATIONAL,
                None,
                Severity.CRITICAL if quality.decision is QualityDecision.REJECT else Severity.HIGH,
                tuple(f"quality:{issue.kind}" for issue in quality.issues) or ("quality:unknown",),
                f"Data Quality Gate {quality.decision.value}",
                "quality issues are resolved and fresh data is validated",
            )
            return triggers

        breakout, trend = values.get("breakout"), values.get("ema_trend")
        mtf_direction = values.get("mtf_direction")
        if breakout == "up" and trend == "up" and mtf_direction != "down":
            add(
                CryptoTriggerGroup.TECHNICAL,
                Stance.BULLISH,
                Severity.HIGH,
                evidence("technical", "feature:technical"),
                "EMA structure and upside breakout are aligned",
                "price falls back below resistance or the EMA structure breaks",
            )
        elif breakout == "down" and trend == "down" and mtf_direction != "up":
            add(
                CryptoTriggerGroup.TECHNICAL,
                Stance.BEARISH,
                Severity.HIGH,
                evidence("technical", "feature:technical"),
                "EMA structure and downside breakdown are aligned",
                "price moves back above support or the EMA structure breaks",
            )

        natr = values.get("normalized_atr_pct")
        if isinstance(natr, (int, float)) and natr >= 4:
            add(
                CryptoTriggerGroup.VOLATILITY,
                None,
                Severity.HIGH,
                evidence("technical", "feature:atr"),
                f"Normalized ATR {natr:.2f}%: high volatility",
                "normalized ATR drops below 4%",
            )

        buy_ratio, imbalance = values.get("aggressive_buy_ratio"), values.get("book_imbalance_10")
        if isinstance(buy_ratio, (int, float)) and isinstance(imbalance, (int, float)):
            if buy_ratio >= 0.65 and imbalance >= 0.15:
                direction = Stance.BULLISH
            elif buy_ratio <= 0.35 and imbalance <= -0.15:
                direction = Stance.BEARISH
            else:
                direction = None
            if direction is not None:
                add(
                    CryptoTriggerGroup.ORDER_FLOW,
                    direction,
                    Severity.MEDIUM,
                    (
                        *evidence("trade_flow", "feature:trades"),
                        *evidence("order_book", "feature:book"),
                    ),
                    "Aggressive trade flow and order-book imbalance point the same way",
                    "flow ratio or book imbalance returns to the neutral band",
                )

        funding = values.get("funding_rate")
        oi_change = values.get("open_interest_change_pct")
        if isinstance(funding, Decimal) and isinstance(oi_change, (int, float)):
            funding_float = float(funding)
            if abs(funding_float) >= 0.001 and oi_change >= 5:
                direction = Stance.BEARISH if funding_float > 0 else Stance.BULLISH
                add(
                    CryptoTriggerGroup.FUNDING_OI,
                    direction,
                    Severity.HIGH,
                    evidence("derivatives", "feature:funding_oi"),
                    "Extreme funding with rising OI: squeeze / crowded-positioning risk",
                    "absolute funding drops below 0.1% or the OI change drops below 5%",
                )

        long_liq = values.get("long_liquidation_notional")
        short_liq = values.get("short_liquidation_notional")
        if isinstance(long_liq, Decimal) and isinstance(short_liq, Decimal):
            total = float(long_liq + short_liq)
            if total > 0 and abs(float(long_liq - short_liq)) / total >= 0.5:
                direction = Stance.BEARISH if long_liq > short_liq else Stance.BULLISH
                add(
                    CryptoTriggerGroup.LIQUIDATION,
                    direction,
                    Severity.HIGH,
                    evidence("liquidations", "feature:liquidations"),
                    "Liquidation flow is concentrated on one side",
                    "long/short liquidation difference drops below 50% of the total",
                )

        if previous_regime is not None and previous_regime is not regime.regime:
            add(
                CryptoTriggerGroup.REGIME,
                (
                    Stance.BULLISH
                    if regime.regime is MarketRegimeKind.TREND_UP
                    else Stance.BEARISH
                    if regime.regime is MarketRegimeKind.TREND_DOWN
                    else None
                ),
                Severity.MEDIUM,
                tuple(f"feature:{name}" for name in regime.features_used) or ("regime:model",),
                f"Regime {previous_regime.value} → {regime.regime.value}",
                "the regime is reclassified",
            )

        if news_event is not None:
            direction, severity, source_id = news_event
            add(
                CryptoTriggerGroup.NEWS_EVENT,
                direction,
                severity,
                (source_id,),
                "Verified news/event trigger",
                "the event effect expires or the verification is withdrawn",
            )
        return triggers


def qualifies_trade_candidate(
    triggers: list[CryptoTrigger],
    *,
    min_independent_groups: int = 2,
) -> bool:
    """Prevent candidates built on a single indicator or the same evidence."""
    if min_independent_groups < 2:
        raise ValueError("a trade candidate requires at least two independent groups")
    if not triggers or any(not item.data_quality.allows_downstream for item in triggers):
        return False
    directional = [item for item in triggers if item.direction in (Stance.BULLISH, Stance.BEARISH)]
    for direction in (Stance.BULLISH, Stance.BEARISH):
        agreeing = {item.group for item in directional if item.direction is direction}
        if len(agreeing) >= min_independent_groups:
            return True
    return False
