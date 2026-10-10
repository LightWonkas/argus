"""Argus error hierarchy.

All custom exceptions derive from :class:`ArgusError`, so upper layers can catch a
single type and degrade gracefully.
"""

from __future__ import annotations


class ArgusError(Exception):
    """Root of all Argus-specific errors."""


class ConfigError(ArgusError):
    """Missing/invalid configuration or secret."""


class DataQualityError(ArgusError):
    """Data that failed the Data Quality Gate (schema/range/outlier/staleness)."""


class AdapterError(ArgusError):
    """Error raised in an external source adapter (L1)."""


class UnsupportedEndpointError(AdapterError):
    """Market/data endpoint the provider explicitly does not support."""


class SequenceGapError(AdapterError):
    """Sequence gap in a WebSocket/order-book stream that cannot be reconciled with the snapshot."""


class ClockSkewError(AdapterError):
    """Exchange time and local UTC time diverged more than allowed."""


class DisconnectedError(AdapterError):
    """The live stream produced no events for the configured period, or closed."""


class RiskRejectedError(ArgusError):
    """Candidate was not processed because of a GUARD or shadow safety limit."""


class BudgetExceededError(ArgusError):
    """The LLM cost governor's daily cap was exceeded."""


class AnalysisError(ArgusError):
    """Error in L3/Brain analysis (LLM call or output parsing)."""


class CircuitOpenError(ArgusError):
    """Circuit breaker open — the source is temporarily disabled (degraded mode)."""


class AuthorizationError(ArgusError):
    """Unauthorized user/operation (authorized chats only)."""


class ConcurrentStateError(ArgusError):
    """Optimistic revision or leader fencing conflict."""
