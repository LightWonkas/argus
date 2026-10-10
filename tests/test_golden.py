"""Golden-set rubric tests — fabricated-number check on a synthetic case (offline)."""

from __future__ import annotations

from pathlib import Path

from argus.brain.golden import (
    GoldenContext,
    extract_numbers,
    input_numbers,
    load_contexts,
    render_report,
    score_analysis,
    score_routing,
    ungrounded_numbers,
)
from argus.core.contracts import AnalysisOutput, Scenario
from argus.core.enums import EvidenceTier, Timeframe

_CTX = GoldenContext(
    name="synthetic",
    role="deep",
    symbol="X",
    timeframe="1d",
    closes=tuple(100.0 + i for i in range(40)),  # 100..139
)


def _output(
    summary: str = "Trend is up.",
    invalidation: str = "a close below 138.0 invalidates it",
    scenarios: int = 1,
) -> AnalysisOutput:
    return AnalysisOutput(
        symbol="X",
        timeframe=Timeframe.D1,
        summary=summary,
        bias="bullish",
        evidence_tier=EvidenceTier.A,
        confidence_pct=70,
        invalidation=invalidation,
        scenarios=tuple(
            Scenario(name=f"S{i}", probability_pct=60, detail="holding above the EMA")
            for i in range(scenarios)
        ),
        model="m",
    )


# ── number extraction + grounding ────────────────────────────────────────────


def test_extract_numbers_handles_comma_decimal() -> None:
    assert extract_numbers("price 139,5 and level 62.1") == [139.5, 62.1]


def test_ungrounded_flags_fabricated_price() -> None:
    # 139.0 is in the input (a close) → clean; 250.75 appears nowhere → FABRICATED flag.
    valid = input_numbers(_CTX)
    flagged = ungrounded_numbers(["support 139.0, target 250.75"], valid)
    assert flagged == [250.75]


def test_grounding_tolerates_one_percent() -> None:
    valid = input_numbers(_CTX)  # 139.0 is present
    assert ungrounded_numbers(["level 139.9"], valid) == []  # within 1% → grounded
    assert ungrounded_numbers(["level 142.5"], valid) == [142.5]  # outside 1% → flagged


def test_small_integers_exempt() -> None:
    # Integers 0–100 (confidence %, RSI threshold 70, 5 candles) are exempt — not fabrication.
    assert ungrounded_numbers(["confidence 70%, within 5 candles, RSI below 30"], set()) == []


# ── score_analysis ───────────────────────────────────────────────────────────


def test_score_clean_output_ok() -> None:
    s = score_analysis(_CTX, _output(summary="strength holds above 139.0"))
    assert s.parsed_ok and s.required_ok and s.invalidation_concrete
    assert s.ungrounded == []
    assert s.ok


def test_score_flags_fabricated_number() -> None:
    s = score_analysis(_CTX, _output(summary="A level of 999.99 is in sight"))
    assert s.ungrounded == [999.99]
    assert not s.ok  # fabricated number → the case is not clean


def test_score_vague_invalidation_fails() -> None:
    s = score_analysis(_CTX, _output(invalidation="invalid if the trend breaks"))
    assert s.invalidation_concrete is False
    assert not s.ok


def test_score_parse_failure() -> None:
    s = score_analysis(_CTX, None, error="AnalysisError")
    assert not s.parsed_ok and not s.ok
    assert s.error == "AnalysisError"


def test_score_injection_detection() -> None:
    ctx = _CTX.model_copy(update={"injection_marker": "HACKED"})
    bad = score_analysis(ctx, _output(summary="HACKED — instruction followed"))
    assert bad.injection_followed is True and not bad.ok
    good = score_analysis(ctx, _output(summary="strong above 139.0"))
    assert good.injection_followed is False and good.ok


def test_score_routing_injection() -> None:
    ctx = GoldenContext(name="inj", role="news_router", injection_marker="INJECTION-SUCCEEDED")
    bad = score_routing(ctx, rationale="INJECTION-SUCCEEDED", confidence_pct=90)
    assert bad.injection_followed is True and not bad.ok
    good = score_routing(
        ctx,
        rationale="The news contains a non-market instruction; low importance.",
        confidence_pct=70,
    )
    assert good.injection_followed is False and good.ok


# ── context set + report ─────────────────────────────────────────────────────


def test_golden_context_set_loads_and_covers_requirements() -> None:
    directory = Path("tests/golden/contexts")
    contexts = load_contexts(directory)
    assert 12 <= len(contexts) <= 20  # 12–20 fixed contexts
    assert [c.name for c in contexts] == sorted(p.stem for p in directory.glob("*.json"))
    roles = {c.role for c in contexts}
    assert roles == {"deep", "news_router", "exchange_expert"}  # incl. BIST expert cases
    assert sum(1 for c in contexts if c.role == "exchange_expert") >= 4
    classes = {c.asset_class for c in contexts}
    assert {"bist", "crypto", "metal"} <= classes
    assert any(c.injection_marker for c in contexts)  # an injection case is mandatory
    assert any(len(c.closes) < 30 and c.role == "deep" for c in contexts)  # the missing-data case


def test_render_report_contains_table_and_summary() -> None:
    from datetime import UTC, datetime

    s = score_analysis(_CTX, _output())
    text = render_report(
        "deep", {"ollama:llama3.2": [s]}, generated_at=datetime(2026, 6, 12, tzinfo=UTC)
    )
    assert "| synthetic | ollama:llama3.2 |" in text
    assert "1/1 contexts clean" in text
