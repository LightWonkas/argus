"""L5 — Execution layer (shadow/dry-run boundary only).

Produces simulated fills/positions/PnL only after a GUARD ``ALLOW_SHADOW`` decision.
This package contains no live exchange order client.
"""

from argus.execution.contracts import (
    AccountBalance,
    AccountSnapshot,
    CapabilityState,
    CommissionProvider,
    ExchangeFill,
    ExchangeOrderSnapshot,
    ExchangeReconciliationService,
    ExecutionOrderStatus,
    PrivateAccountDataClient,
    PublicMarketDataClient,
    ReconciliationReport,
    SpotExecutionClient,
    SubmissionAttempt,
    UserDataEvent,
    UserDataStreamClient,
)
from argus.execution.shadow import ShadowEngine, ShadowExecution, ShadowStore

__all__ = [
    "AccountBalance",
    "AccountSnapshot",
    "CapabilityState",
    "CommissionProvider",
    "ExchangeFill",
    "ExchangeOrderSnapshot",
    "ExchangeReconciliationService",
    "ExecutionOrderStatus",
    "PrivateAccountDataClient",
    "PublicMarketDataClient",
    "ReconciliationReport",
    "ShadowEngine",
    "ShadowExecution",
    "ShadowStore",
    "SpotExecutionClient",
    "SubmissionAttempt",
    "UserDataEvent",
    "UserDataStreamClient",
]
