"""Command-line entry point for AutoLab."""

from __future__ import annotations

import argparse
import asyncio
import sys
from uuid import uuid4

from autolab import __version__
from autolab.config import get_settings


def main(argv: list[str] | None = None) -> None:
    """`autolab` CLI entry.

    - With no args: prints the version banner (smoke check).
    - With `run "<question>"`: runs the graph against Claude. Exits with
      status 2 and a message on stderr if `ANTHROPIC_API_KEY` is not set.
    """
    parser = argparse.ArgumentParser(prog="autolab")
    sub = parser.add_subparsers(dest="cmd")

    run_p = sub.add_parser("run", help="Run a research question end-to-end")
    run_p.add_argument("question", help="The research question to investigate")

    args = parser.parse_args(argv)

    if args.cmd is None:
        sys.stdout.write(f"AutoLab v{__version__} - skeleton OK\n")
        return

    if args.cmd == "run":
        api_key = get_settings().anthropic_api_key
        if not api_key:
            sys.stderr.write(
                "autolab run: ANTHROPIC_API_KEY is not set. "
                "Export it or add it to .env (see .env.example).\n"
            )
            raise SystemExit(2)
        asyncio.run(_run(args.question, api_key))


async def _run(question: str, api_key: str) -> None:
    # Lazy imports so the no-arg banner stays light.
    from autolab.orchestrator.graph import build_graph
    from autolab.state.schema import empty_state
    from autolab.verification.llm import ClaudeLLMClient

    graph = build_graph(llm=ClaudeLLMClient(api_key))
    config = {"configurable": {"thread_id": str(uuid4())}}
    final = await graph.ainvoke(empty_state(question), config=config)
    pending = final.get("hitl_pending")
    if "__interrupt__" in final and pending is not None:
        sys.stdout.write(f"Paused for human review ({pending.agent}): {pending.reason}\n")
        return
    sys.stdout.write(
        f"Run complete. confidence={final.get('confidence', 0.0):.2f}, "
        f"hypotheses={len(final.get('hypotheses', []))}\n"
    )
