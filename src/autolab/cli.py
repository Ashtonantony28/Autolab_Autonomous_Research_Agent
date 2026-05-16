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
    - With `run "<question>"`: runs the graph against a `FakeLLMClient`
      (Phase 7 does not exercise the live Claude path in tests; flip the
      `RUN_LIVE_TESTS` env var to opt in once an API key is configured).
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
        asyncio.run(_run(args.question))


async def _run(question: str) -> None:
    # Lazy imports so the no-arg banner stays light.
    from autolab.orchestrator.graph import build_graph
    from autolab.state.schema import empty_state
    from autolab.verification.llm import ClaudeLLMClient, FakeLLMClient

    settings = get_settings()
    llm = (
        ClaudeLLMClient(settings.anthropic_api_key)
        if settings.anthropic_api_key
        else FakeLLMClient()
    )
    graph = build_graph(llm=llm)
    config = {"configurable": {"thread_id": str(uuid4())}}
    final = await graph.ainvoke(empty_state(question), config=config)
    sys.stdout.write(
        f"Run complete. confidence={final.get('confidence', 0.0):.2f}, "
        f"hypotheses={len(final.get('hypotheses', []))}\n"
    )
