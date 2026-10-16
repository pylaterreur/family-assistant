#!/usr/bin/env python3
"""Run the eval-only ``allow_watched`` script-review prototype against a judge.

Measures the program-review outcome proposed in
docs/design/script-execution-review-boundary.md without changing production:
see ``family_assistant.eval.tool_call_review.script_review_prototype``.

``--prompt prototype`` (default) sends the prototype script-review prompt with
the four-outcome verdict space; ``--prompt production`` sends today's prompt
unchanged as a baseline. Prints totals and a per-case table as Markdown, and
with ``--out`` writes every trial, reasons included, as JSON.

Usage:

    # Validate and render the cases, no network:
    python scripts/script_review_prototype_eval.py --dry-run

    # Measure the deployed judge model on the prototype prompt:
    python scripts/script_review_prototype_eval.py \
        --provider anthropic --model claude-haiku-5-5 --seeds 5 \
        --out scratch/script-review-prototype.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from family_assistant.eval.tool_call_review.script_review_prototype import (
    PromptVariant,
    TrialExecutionError,
    assemble_prototype_messages,
    default_cases_dir,
    load_prototype_cases,
    prototype_registry,
    render_markdown_summary,
    run_prototype_eval,
)
from family_assistant.llm.factory import LLMClientFactory


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=default_cases_dir())
    parser.add_argument(
        "--prompt", choices=("prototype", "production"), default="prototype"
    )
    parser.add_argument("--provider", default="anthropic")
    parser.add_argument("--model", default="claude-haiku-5-5")
    parser.add_argument("--seeds", type=int, default=5)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--case",
        action="append",
        default=None,
        metavar="ID",
        help="Run only this case id (repeatable).",
    )
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    variant: PromptVariant = args.prompt
    registry = prototype_registry()
    cases = load_prototype_cases(args.cases)
    if args.case:
        wanted = set(args.case)
        cases = [case for case in cases if case.id in wanted]
        missing = wanted - {case.id for case in cases}
        if missing:
            print(f"Unknown case ids: {sorted(missing)}", file=sys.stderr)
            return 1
    for case in cases:
        assemble_prototype_messages(case, variant, registry)
    print(f"Loaded {len(cases)} case(s); prompt={variant}")
    if args.dry_run:
        for case in cases:
            print(f"  {case.id} [{case.family}/{case.label}]")
        return 0

    client = LLMClientFactory.create_client({
        "provider": args.provider,
        "model": args.model,
        "model_parameters": {},
    })
    try:
        trials = await run_prototype_eval(
            cases,
            client,
            variant=variant,
            seeds=args.seeds,
            registry=registry,
            concurrency=args.concurrency,
        )
    except TrialExecutionError as exc:
        print(f"Run aborted: {exc}", file=sys.stderr)
        return 1
    print(f"\njudge={args.provider}/{args.model} seeds={args.seeds}\n")
    print(render_markdown_summary(cases, trials))
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                {
                    "provider": args.provider,
                    "model": args.model,
                    "prompt": variant,
                    "seeds": args.seeds,
                    "trials": [trial.model_dump(mode="json") for trial in trials],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nWrote {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point returning a process exit code."""
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
