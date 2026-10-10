"""LLM cost estimation tests."""

from __future__ import annotations

import pytest

from argus.brain import estimate_cost


def test_estimate_cost_opus() -> None:
    # opus-4-8: input $5/1M, output $25/1M → 1M+1M = $30
    assert estimate_cost(
        "claude-opus-4-8", input_tokens=1_000_000, output_tokens=1_000_000
    ) == pytest.approx(30.0)


def test_estimate_cost_sonnet() -> None:
    # sonnet-4-6: input $3/1M, output $15/1M
    assert estimate_cost(
        "claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=0
    ) == pytest.approx(3.0)


def test_estimate_cost_cache_discount() -> None:
    # all input read from cache → 0.1× ($5 × 0.1 = $0.5)
    assert estimate_cost(
        "claude-opus-4-8", input_tokens=1_000_000, output_tokens=0, cache_read_tokens=1_000_000
    ) == pytest.approx(0.5)


def test_estimate_cost_unknown_model_is_zero() -> None:
    assert estimate_cost("unknown-model", input_tokens=1000, output_tokens=1000) == 0.0
