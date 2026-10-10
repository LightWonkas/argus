#!/usr/bin/env python
"""Golden eval CLI — provider/model quality comparison on fixed contexts.

Usage:
    python scripts/golden_eval.py --role deep --provider ollama --model llama3.2
    python scripts/golden_eval.py --role deep --provider claude --model claude-opus-4-8
    python scripts/golden_eval.py --role news_router --provider ollama --model llama3.2 \
        --compare claude:claude-sonnet-4-6        # two models side by side

Output: ``reports/golden_<role>_<date>.md`` + a stdout summary. RULE: a role->model change
is never made without running this eval first (so quality cannot degrade silently).
Cost: Ollama is $0; a Claude run spends real API budget (the CostGuard ceiling applies).
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

if sys.platform == "win32":  # pragma: no cover — legacy console code page
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")

from argus.brain.golden import (
    GoldenContext,
    GoldenScore,
    build_news_items,
    build_series,
    load_contexts,
    render_report,
    score_analysis,
    score_opinion,
    score_routing,
)
from argus.config.settings import Settings, get_settings

_CONTEXT_DIR = Path("tests/golden/contexts")


def _settings_for(base: Settings, role: str, provider: str, model: str) -> Settings:
    """Settings copy that temporarily overrides the role's provider + model."""
    return base.model_copy(update={f"provider_{role}": provider, f"model_{role}": model})


def _run_deep(cfg: Settings, contexts: list[GoldenContext]) -> list[GoldenScore]:
    from argus.brain.factory import build_analyst
    from argus.platform.triggers import TriggerEngine

    analyst = build_analyst(cfg)
    scores: list[GoldenScore] = []
    for ctx in contexts:
        series = build_series(ctx)
        signals = TriggerEngine().evaluate(series)
        try:
            output = analyst.analyze(series, signals, use_web_search=False)
        except Exception as exc:  # parse/network/budget -> scored as a schema FAIL
            scores.append(score_analysis(ctx, None, error=type(exc).__name__))
            print(f"  ❌ {ctx.name}: {type(exc).__name__}")
            continue
        score = score_analysis(ctx, output)
        scores.append(score)
        flag = "✅" if score.ok else "⚠️"
        print(f"  {flag} {ctx.name} (ungrounded: {len(score.ungrounded)})")
    return scores


def _run_news_router(cfg: Settings, contexts: list[GoldenContext]) -> list[GoldenScore]:
    from argus.brain.news_router import build_news_router

    router = build_news_router(cfg)
    if router.demo:
        print("  ⊘ router is in demo mode — no real eval is possible for this provider")
        return [
            score_routing(c, rationale="", confidence_pct=None, status="demo", error="demo mode")
            for c in contexts
        ]
    scores: list[GoldenScore] = []
    for ctx in contexts:
        items = build_news_items(ctx)
        if not items:
            continue
        item = items[0]
        try:
            label = router.route(
                item.title,
                summary=item.summary,
                source=item.source_name,
                symbols=list(item.symbols),
            )
            score = score_routing(
                ctx, rationale=label.rationale, confidence_pct=label.confidence_pct
            )
        except Exception as exc:
            score = score_routing(
                ctx, rationale="", confidence_pct=None, status="error", error=type(exc).__name__
            )
        scores.append(score)
        flag = "✅" if score.ok else "⚠️"
        print(f"  {flag} {ctx.name}")
    return scores


def _run_exchange_expert(cfg: Settings, contexts: list[GoldenContext]) -> list[GoldenScore]:
    from argus.brain.agents import AnalysisContext
    from argus.brain.expert_bist import build_bist_expert_agent
    from argus.core.enums import AssetClass, Timeframe

    expert = build_bist_expert_agent(cfg)
    if expert.demo:
        print("  ⊘ expert is in demo mode — no real eval is possible for this provider")
        return [
            score_opinion(c, stance="", rationale="", confidence_pct=0, error="demo mode")
            for c in contexts
        ]
    scores: list[GoldenScore] = []
    for ctx in contexts:
        a_ctx = AnalysisContext(
            symbol=ctx.symbol,
            timeframe=Timeframe(ctx.timeframe),
            series=build_series(ctx),
            news=tuple(build_news_items(ctx)),
            asset_class=AssetClass(ctx.asset_class),
        )
        try:
            opinion = expert.assess(a_ctx)
            score = score_opinion(
                ctx,
                stance=opinion.stance.value,
                rationale=opinion.rationale,
                confidence_pct=opinion.confidence_pct,
            )
        except Exception as exc:
            score = score_opinion(
                ctx, stance="", rationale="", confidence_pct=0, error=type(exc).__name__
            )
        scores.append(score)
        flag = "✅" if score.ok else "⚠️"
        print(f"  {flag} {ctx.name} (ungrounded: {len(score.ungrounded)})")
    return scores


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="golden_eval", description="Argus golden set eval")
    parser.add_argument(
        "--role", choices=("deep", "news_router", "exchange_expert"), default="deep"
    )
    parser.add_argument("--provider", required=True, help="claude | ollama")
    parser.add_argument("--model", required=True, help="model name (e.g. llama3.2)")
    parser.add_argument(
        "--compare",
        default="",
        metavar="PROVIDER:MODEL",
        help="Second model (side-by-side comparison), e.g. claude:claude-sonnet-4-6",
    )
    parser.add_argument("--out-dir", default="reports", help="Report directory (default: reports)")
    args = parser.parse_args(argv)

    contexts = load_contexts(_CONTEXT_DIR, role=args.role)
    if not contexts:
        print(f"ERROR: no '{args.role}' contexts under {_CONTEXT_DIR}.", file=sys.stderr)
        return 2
    print(f"Golden eval — role={args.role} · {len(contexts)} contexts")

    runs: list[tuple[str, str]] = [(args.provider, args.model)]
    if args.compare:
        provider2, _, model2 = args.compare.partition(":")
        if not model2:
            print("ERROR: --compare must have the form PROVIDER:MODEL.", file=sys.stderr)
            return 2
        runs.append((provider2, model2))

    base = get_settings()
    results: dict[str, list[GoldenScore]] = {}
    runners = {
        "deep": _run_deep,
        "news_router": _run_news_router,
        "exchange_expert": _run_exchange_expert,
    }
    runner = runners[args.role]
    for provider, model in runs:
        label = f"{provider}:{model}"
        print(f"\n── {label} ──")
        cfg = _settings_for(base, args.role, provider, model)
        results[label] = runner(cfg, contexts)

    now = datetime.now(UTC)
    text = render_report(args.role, results, generated_at=now)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"golden_{args.role}_{now:%Y%m%d_%H%M}.md"
    out_path.write_text(text, encoding="utf-8")
    print(f"\nReport written: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
