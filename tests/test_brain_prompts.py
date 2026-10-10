"""Brain prompt + output parsing tests."""

from __future__ import annotations

import pytest

from argus.brain import build_system_prompt, parse_analysis
from argus.brain.prompts import build_user_prompt
from argus.core.enums import EvidenceTier, Timeframe
from argus.core.errors import AnalysisError
from argus.core.schemas import DATA_PENDING


def test_system_prompt_has_disclaimer_and_tiers() -> None:
    p = build_system_prompt()
    assert "never financial advice" in p.lower()
    assert "[A]" in p and "[B]" in p and "[C]" in p
    assert "DATA PENDING" in p


def test_user_prompt_contains_context() -> None:
    p = build_user_prompt(
        symbol="BTC/USDT",
        timeframe=Timeframe.H1,
        last_close=100.0,
        triggers=["rsi_overbought [medium] RSI 77"],
        indicators={"RSI(14)": 77.0, "ATR(14)": None},
    )
    assert "BTC/USDT" in p
    assert "rsi_overbought" in p
    assert "77.00" in p
    assert DATA_PENDING in p  # ATR None → DATA PENDING


def test_parse_valid_json() -> None:
    text = (
        '{"summary":"overview","bias":"bullish","evidence_tier":"A","confidence_pct":70,'
        '"invalidation":"close below 100",'
        '"scenarios":[{"name":"Base","probability_pct":60,"detail":"up"}]}'
    )
    out = parse_analysis(text, symbol="BTC/USDT", timeframe=Timeframe.H1, model="m")
    assert out.evidence_tier is EvidenceTier.A
    assert out.confidence_pct == 70
    assert out.bias == "bullish"
    assert len(out.scenarios) == 1
    assert "not financial advice" in out.disclaimer.lower()


def test_parse_fenced_json() -> None:
    text = (
        '```json\n{"summary":"s","evidence_tier":"B","confidence_pct":50,"invalidation":"y"}\n```'
    )
    out = parse_analysis(text, symbol="X", timeframe=Timeframe.D1, model="m")
    assert out.evidence_tier is EvidenceTier.B
    assert out.scenarios == ()


def test_parse_missing_fields_safe_defaults() -> None:
    out = parse_analysis('{"foo":"bar"}', symbol="X", timeframe=Timeframe.D1, model="m")
    assert out.summary == DATA_PENDING
    assert out.invalidation == DATA_PENDING
    assert out.evidence_tier is EvidenceTier.C  # conservative when unknown
    assert out.confidence_pct == 0


def test_parse_confidence_clamped() -> None:
    text = '{"summary":"s","evidence_tier":"A","confidence_pct":150,"invalidation":"y"}'
    out = parse_analysis(text, symbol="X", timeframe=Timeframe.D1, model="m")
    assert out.confidence_pct == 100


def test_parse_no_json_raises() -> None:
    with pytest.raises(AnalysisError):
        parse_analysis("no json here", symbol="X", timeframe=Timeframe.D1, model="m")
