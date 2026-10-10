"""Deterministic Binance spot universe eligibility and research ranking."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Final, cast

from argus.config.models import UniverseDiscoveryConfig
from argus.core.schemas import DataStatus, utcnow
from argus.core.trading import (
    AssetCategory,
    AssetTaxonomyEntry,
    ResearchStatus,
    SymbolResearchScore,
    UniverseEligibilitySnapshot,
    UniverseMarketObservation,
    UniverseRejectReason,
)

_HUNDRED: Final = Decimal("100")


def _decimal(value: float | int | str | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


class AssetTaxonomy:
    """Versioned explicit registry; never guesses a class from a symbol suffix."""

    VERSION = "asset-taxonomy-v1"
    _STABLECOINS = frozenset(
        {
            "BUSD",
            "DAI",
            "FDUSD",
            "FRAX",
            "GUSD",
            "PYUSD",
            "RLUSD",
            "TUSD",
            "USD1",
            "USDC",
            "USDD",
            "USDE",
            "USDF",
            "USDP",
            "USDS",
            "USDT",
            "XUSD",
        }
    )
    _FIAT = frozenset({"AUD", "BRL", "EUR", "GBP", "JPY", "TRY", "UAH", "ZAR"})
    # Explicit product records historically used on Binance. The "UP/DOWN" suffix alone
    # is never used for classification; avoiding false positives is a safety feature.
    _LEVERAGED = frozenset(
        {
            "ADADOWN",
            "ADAUP",
            "BNBDOWN",
            "BNBUP",
            "BTCDOWN",
            "BTCUP",
            "ETHDOWN",
            "ETHUP",
            "LINKDOWN",
            "LINKUP",
            "SUSHIDOWN",
            "SUSHIUP",
            "TRXDOWN",
            "TRXUP",
            "XRPDOWN",
            "XRPUP",
        }
    )

    def __init__(self, entries: tuple[AssetTaxonomyEntry, ...] = ()) -> None:
        self._entries = {entry.asset: entry for entry in entries}

    def classify(self, asset: str) -> AssetTaxonomyEntry:
        normalized = asset.strip().upper()
        explicit = self._entries.get(normalized)
        if explicit is not None:
            return explicit
        if normalized in self._STABLECOINS:
            category = AssetCategory.STABLECOIN
        elif normalized in self._LEVERAGED:
            category = AssetCategory.LEVERAGED_TOKEN
        elif normalized in self._FIAT:
            category = AssetCategory.FIAT
        else:
            category = AssetCategory.CRYPTO
        return AssetTaxonomyEntry(
            asset=normalized,
            category=category,
            source="argus-explicit-registry",
            version=self.VERSION,
        )


class UniverseDiscoveryEngine:
    """Applies the hard filters first; only eligible symbols can be scored."""

    POLICY_VERSION = "universe-policy-v1"

    def __init__(
        self,
        config: UniverseDiscoveryConfig,
        *,
        taxonomy: AssetTaxonomy | None = None,
    ) -> None:
        self._config = config
        self._taxonomy = taxonomy or AssetTaxonomy()

    def broad_rejection_reasons(
        self, observation: UniverseMarketObservation
    ) -> tuple[UniverseRejectReason, ...]:
        """Blocking conditions that a cheap broad scan can prove."""
        category = self._taxonomy.classify(observation.base_asset).category
        reasons: list[UniverseRejectReason] = []

        def reject(condition: bool, reason: UniverseRejectReason) -> None:
            if condition and reason not in reasons:
                reasons.append(reason)

        reject(
            observation.quote_asset != self._config.quote_asset,
            UniverseRejectReason.QUOTE_NOT_USDT,
        )
        reject(observation.exchange_status != "TRADING", UniverseRejectReason.NOT_TRADING)
        reject(not observation.spot_allowed, UniverseRejectReason.SPOT_NOT_ALLOWED)
        reject(category is AssetCategory.STABLECOIN, UniverseRejectReason.STABLECOIN)
        reject(category is AssetCategory.LEVERAGED_TOKEN, UniverseRejectReason.LEVERAGED_TOKEN)
        reject(observation.symbol in self._config.blacklist, UniverseRejectReason.BLACKLISTED)
        reject(
            observation.filters is None or not observation.filters.complete_for_spot_order,
            UniverseRejectReason.FILTERS_UNKNOWN,
        )
        reject(
            observation.quote_volume_24h is None,
            UniverseRejectReason.METRIC_NO_DATA,
        )
        reject(
            observation.quote_volume_24h is not None
            and observation.quote_volume_24h < _decimal(self._config.minimum_quote_volume_24h),
            UniverseRejectReason.LOW_LIQUIDITY,
        )
        reject(observation.spread_bps is None, UniverseRejectReason.METRIC_NO_DATA)
        reject(
            observation.spread_bps is not None
            and observation.spread_bps > _decimal(self._config.maximum_spread_bps),
            UniverseRejectReason.HIGH_SPREAD,
        )
        reject(
            observation.data_status in {DataStatus.QUARANTINED, DataStatus.REJECTED},
            (
                UniverseRejectReason.DATA_QUARANTINED
                if observation.data_status is DataStatus.QUARANTINED
                else UniverseRejectReason.DATA_REJECTED
            ),
        )
        return tuple(reasons)

    def evaluate(
        self,
        observation: UniverseMarketObservation,
        *,
        discovered_at: datetime | None = None,
        maximum_trade_notional: Decimal | None = None,
    ) -> UniverseEligibilitySnapshot:
        at = discovered_at or utcnow()
        taxonomy = self._taxonomy.classify(observation.base_asset)
        reasons: list[UniverseRejectReason] = []

        def reject(condition: bool, reason: UniverseRejectReason) -> None:
            if condition and reason not in reasons:
                reasons.append(reason)

        reject(
            observation.quote_asset != self._config.quote_asset,
            UniverseRejectReason.QUOTE_NOT_USDT,
        )
        reject(observation.exchange_status != "TRADING", UniverseRejectReason.NOT_TRADING)
        reject(not observation.spot_allowed, UniverseRejectReason.SPOT_NOT_ALLOWED)
        reject(taxonomy.category is AssetCategory.STABLECOIN, UniverseRejectReason.STABLECOIN)
        reject(
            taxonomy.category is AssetCategory.LEVERAGED_TOKEN,
            UniverseRejectReason.LEVERAGED_TOKEN,
        )
        reject(observation.symbol in self._config.blacklist, UniverseRejectReason.BLACKLISTED)
        reject(observation.filters is None, UniverseRejectReason.FILTERS_UNKNOWN)
        if observation.filters is not None:
            reject(
                not observation.filters.complete_for_spot_order,
                UniverseRejectReason.FILTERS_UNKNOWN,
            )
            if maximum_trade_notional is not None:
                minimum = observation.filters.min_notional
                reject(
                    minimum is not None and minimum > maximum_trade_notional,
                    UniverseRejectReason.MIN_NOTIONAL_INCOMPATIBLE,
                )

        listing_age: Decimal | None = None
        if observation.listing_time is None:
            reject(
                self._config.reject_unknown_listing_age,
                UniverseRejectReason.LISTING_AGE_UNKNOWN,
            )
        else:
            listing_age = _decimal((at - observation.listing_time).total_seconds()) / Decimal(
                "86400"
            )
            reject(
                listing_age < self._config.minimum_listing_age_days,
                UniverseRejectReason.NEW_LISTING,
            )

        required_metrics = (
            observation.quote_volume_24h,
            observation.history_bars,
            observation.spread_bps,
            observation.estimated_depth_quote,
            observation.estimated_slippage_bps,
            observation.realized_volatility_pct,
        )
        reject(
            any(value is None for value in required_metrics), UniverseRejectReason.METRIC_NO_DATA
        )
        reject(
            observation.quote_volume_24h is not None
            and observation.quote_volume_24h < _decimal(self._config.minimum_quote_volume_24h),
            UniverseRejectReason.LOW_LIQUIDITY,
        )
        reject(
            observation.history_bars is not None
            and observation.history_bars < self._config.minimum_history_bars,
            UniverseRejectReason.INSUFFICIENT_HISTORY,
        )
        reject(
            observation.spread_bps is not None
            and observation.spread_bps > _decimal(self._config.maximum_spread_bps),
            UniverseRejectReason.HIGH_SPREAD,
        )
        reject(
            observation.estimated_depth_quote is not None
            and observation.estimated_depth_quote < _decimal(self._config.minimum_depth_quote),
            UniverseRejectReason.LOW_DEPTH,
        )
        reject(
            observation.estimated_slippage_bps is not None
            and observation.estimated_slippage_bps > _decimal(self._config.maximum_slippage_bps),
            UniverseRejectReason.HIGH_SLIPPAGE,
        )
        reject(
            observation.realized_volatility_pct is not None
            and observation.realized_volatility_pct
            > _decimal(self._config.maximum_realized_volatility_pct),
            UniverseRejectReason.MARKET_ANOMALY,
        )
        reject(bool(observation.anomaly_flags), UniverseRejectReason.MARKET_ANOMALY)
        reject(
            observation.data_status is DataStatus.PENDING,
            UniverseRejectReason.METRIC_NO_DATA,
        )
        reject(
            observation.data_status is DataStatus.QUARANTINED,
            UniverseRejectReason.DATA_QUARANTINED,
        )
        reject(
            observation.data_status is DataStatus.REJECTED,
            UniverseRejectReason.DATA_REJECTED,
        )

        return UniverseEligibilitySnapshot(
            symbol=observation.symbol,
            base_asset=observation.base_asset,
            quote_asset=observation.quote_asset,
            discovered_at=at,
            eligible=not reasons,
            rejection_reasons=tuple(reasons),
            listing_age_days=listing_age,
            listing_age_source=observation.listing_time_source,
            quote_volume_24h=observation.quote_volume_24h,
            trade_count_24h=observation.trade_count_24h,
            spread_bps=observation.spread_bps,
            estimated_depth_quote=observation.estimated_depth_quote,
            estimated_slippage_bps=observation.estimated_slippage_bps,
            realized_volatility_pct=observation.realized_volatility_pct,
            history_bars=observation.history_bars,
            filters=observation.filters,
            quality_status=observation.data_status,
            taxonomy_category=taxonomy.category,
            policy_version=self.POLICY_VERSION,
        )

    @staticmethod
    def score(
        eligibility: UniverseEligibilitySnapshot,
        *,
        liquidity: Decimal | None,
        trend: Decimal | None,
        momentum: Decimal | None,
        order_flow: Decimal | None,
        regime_compatibility: Decimal | None,
        execution_quality: Decimal | None,
        risk: Decimal | None,
        manipulation_risk: Decimal | None,
        cost_adjusted_edge: Decimal | None,
        portfolio_fit: Decimal | None,
        evidence: tuple[str, ...],
        data_sources: tuple[str, ...],
        model_version: str = "research-score-v1",
    ) -> SymbolResearchScore:
        values = (
            liquidity,
            trend,
            momentum,
            order_flow,
            regime_compatibility,
            execution_quality,
            risk,
            manipulation_risk,
            cost_adjusted_edge,
            portfolio_fit,
        )
        if not eligibility.eligible:
            return SymbolResearchScore(
                symbol=eligibility.symbol,
                as_of=eligibility.discovered_at,
                status=ResearchStatus.REJECT,
                evidence=evidence,
                risk_flags=tuple(reason.value for reason in eligibility.rejection_reasons),
                data_sources=data_sources,
                model_or_rule_version=model_version,
            )
        if any(value is None for value in values):
            return SymbolResearchScore(
                symbol=eligibility.symbol,
                as_of=eligibility.discovered_at,
                status=ResearchStatus.NO_DATA,
                evidence=evidence,
                risk_flags=("research_metric_no_data",),
                data_sources=data_sources,
                model_or_rule_version=model_version,
            )
        complete = tuple(value for value in values if value is not None)
        if any(value < 0 or value > _HUNDRED for value in complete):
            raise ValueError("research scores must be within 0..100")
        # High risk and manipulation must not raise the score.
        safe_risk = _HUNDRED - complete[6]
        safe_manipulation = _HUNDRED - complete[7]
        weighted = (
            complete[0] * Decimal("0.15")
            + complete[1] * Decimal("0.10")
            + complete[2] * Decimal("0.10")
            + complete[3] * Decimal("0.10")
            + complete[4] * Decimal("0.10")
            + complete[5] * Decimal("0.15")
            + safe_risk * Decimal("0.10")
            + safe_manipulation * Decimal("0.05")
            + complete[8] * Decimal("0.10")
            + complete[9] * Decimal("0.05")
        )
        return SymbolResearchScore(
            symbol=eligibility.symbol,
            as_of=eligibility.discovered_at,
            status=ResearchStatus.OK,
            eligibility_score=_HUNDRED,
            liquidity_score=complete[0],
            trend_score=complete[1],
            momentum_score=complete[2],
            order_flow_score=complete[3],
            regime_compatibility_score=complete[4],
            execution_quality_score=complete[5],
            risk_score=complete[6],
            manipulation_risk_score=complete[7],
            cost_adjusted_edge_score=complete[8],
            portfolio_fit_score=complete[9],
            final_research_score=weighted,
            evidence=evidence,
            data_sources=data_sources,
            model_or_rule_version=model_version,
        )

    @staticmethod
    def rank(scores: tuple[SymbolResearchScore, ...]) -> tuple[SymbolResearchScore, ...]:
        eligible = (
            score
            for score in scores
            if score.status is ResearchStatus.OK and score.final_research_score is not None
        )
        return tuple(
            sorted(
                eligible,
                key=lambda item: (-cast(Decimal, item.final_research_score), item.symbol),
            )
        )
