"""Command-line entry point for AutoLab."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from uuid import uuid4

from autolab import __version__
from autolab.config import get_settings

if TYPE_CHECKING:
    from autolab.verification.llm import LLMClient

DEFAULT_CHECKPOINTS: Final[str] = "autolab_checkpoints.db"
"""SQLite file holding LangGraph checkpoints, so a paused run can be resumed later."""


def main(argv: list[str] | None = None) -> None:
    """`autolab` CLI entry.

    - With no args: prints the version banner (smoke check).
    - With `run "<question>"`: runs the graph against Claude. Exits with
      status 2 and a message on stderr if `ANTHROPIC_API_KEY` is not set.
    - With `resume <thread_id> approve|retry|abort [--note ...]`: resumes a
      run paused for human review. Exits with status 2 if it is not paused.
    """
    parser = argparse.ArgumentParser(prog="autolab")
    sub = parser.add_subparsers(dest="cmd")

    run_p = sub.add_parser("run", help="Run a research question end-to-end")
    run_p.add_argument("question", help="The research question to investigate")

    resume_p = sub.add_parser("resume", help="Resume a run paused for human review")
    resume_p.add_argument("thread_id", help="The thread id printed when the run paused")
    resume_p.add_argument("action", choices=("approve", "retry", "abort"))
    resume_p.add_argument("--note", default="", help="Feedback for the agent on retry")

    for p in (run_p, resume_p):
        p.add_argument(
            "--checkpoints", type=Path, default=Path(DEFAULT_CHECKPOINTS), help="SQLite file"
        )

    args = parser.parse_args(argv)

    if args.cmd is None:
        sys.stdout.write(f"AutoLab v{__version__} - skeleton OK\n")
        return

    api_key = get_settings().anthropic_api_key
    if not api_key:
        sys.stderr.write(
            f"autolab {args.cmd}: ANTHROPIC_API_KEY is not set. "
            "Export it or add it to .env (see .env.example).\n"
        )
        raise SystemExit(2)
    llm = _make_llm(api_key)

    if args.cmd == "run":
        from autolab.state.schema import empty_state

        asyncio.run(_drive(llm, args.checkpoints, str(uuid4()), empty_state(args.question)))
        return

    from langgraph.types import Command

    from autolab.state.models import HITLResolution

    resolution = HITLResolution(action=args.action, note=args.note)
    asyncio.run(
        _drive(llm, args.checkpoints, args.thread_id, Command(resume=resolution), resume=True)
    )


def _make_llm(api_key: str) -> LLMClient:
    from autolab.verification.llm import ClaudeLLMClient

    return ClaudeLLMClient(api_key)


async def _drive(
    llm: LLMClient, checkpoints: Path, thread_id: str, graph_input: Any, *, resume: bool = False
) -> None:
    """Run (or resume) `thread_id` to its end or next HITL pause, tracing to the store."""
    # Lazy imports so the no-arg banner stays light.
    from autolab.monitoring.tracer import RunTracer
    from autolab.orchestrator.graph import build_graph
    from autolab.state.checkpointer import sqlite_saver
    from autolab.state.store import StateStore

    store = await StateStore.create(get_settings().database_url)
    try:
        async with sqlite_saver(checkpoints) as saver:
            graph = build_graph(llm=llm, checkpointer=saver, tracer=RunTracer(store))
            config = {"configurable": {"thread_id": thread_id}}
            if resume and not (await graph.aget_state(config)).interrupts:
                sys.stderr.write(f"autolab resume: no paused run {thread_id} in {checkpoints}\n")
                raise SystemExit(2)
            final = await graph.ainvoke(graph_input, config=config)
    finally:
        await store.dispose()

    pending = final.get("hitl_pending")
    if "__interrupt__" in final and pending is not None:
        sys.stdout.write(
            f"Paused for human review ({pending.agent}): {pending.reason}\n"
            f"Resume with: autolab resume {thread_id} approve|retry|abort [--note ...]\n"
        )
        return
    sys.stdout.write(
        f"Run {thread_id} complete. confidence={final.get('confidence', 0.0):.2f}, "
        f"hypotheses={len(final.get('hypotheses', []))}\n"
    )
