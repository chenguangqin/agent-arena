"""Command line for agent.py: interactive chat, one-shot prompt, or the scenario task.

    uv run python agent.py                       # chat
    uv run python agent.py -p "问题"             # one question, then exit
    uv run python agent.py --task                # run the scenario task once and show the result
    uv run python agent.py --mode tool ...       # select a mode your build_agent() understands
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from labkit import trace
from labkit.env import Env

# build_agent(env, mode) returns either a Strands Agent or any object with
# __call__(message) -> result (str() gives the reply) and, optionally, cleanup() and tool_names.
BuildAgent = Callable[[Env, str], Any]
HELP = """commands:
  /task     send the scenario task
  /tools    tools the model sees
  /stats    tokens, peak context, tool calls (per agent in by_agent)
  /state    final state of the store (what actually happened)
  /trace    last 10 trace events
  /reset    fresh agent and fresh store
  /quit     exit"""


def run_cli(build_agent: BuildAgent, modes: list[str], argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lab agent")
    parser.add_argument("--mode", default=modes[0], choices=modes)
    parser.add_argument("-p", "--prompt", help="ask one question and exit")
    parser.add_argument("--task", action="store_true", help="run the scenario task once and exit")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)

    # One file per run, so parallel or repeated runs never mix their events.
    trace_path = Path("reports") / f"trace-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    trace.configure(trace_path, echo=True)
    while True:
        env = Env.local(args.seed)
        agent = _build(build_agent, env, args.mode)
        try:
            if args.prompt or args.task:
                _ask(agent, env.instruction if args.task else args.prompt)
                _print_summary(env)
                return 0
            print(f"mode={args.mode} · trace → {trace_path}\n{HELP}\n")
            if _repl(agent, env, trace_path) == "quit":
                return 0
            print("\n--- reset ---\n")
        finally:
            cleanup(agent)
            env.close()


def _build(build_agent: BuildAgent, env: Env, mode: str) -> Any:
    agent = build_agent(env, mode)
    attach_tracer(agent)
    trace.reset_totals()
    return agent


def attach_tracer(agent: Any) -> None:
    """A plain Strands Agent gets the tracer here; a workflow runner traces its own agents."""
    hooks = getattr(agent, "hooks", None)
    if hooks is not None and hasattr(hooks, "add_hook"):
        hooks.add_hook(trace.Tracer())


def cleanup(agent: Any) -> None:
    if hasattr(agent, "cleanup"):
        agent.cleanup()


def _ask(agent: Any, message: str) -> None:
    print(f"\nagent> {invoke(agent, message)}\n")


def invoke(agent: Any, message: str) -> str:
    """Run one request. A response cut off by max_tokens ends the run; it is reported, not raised."""
    from strands.types.exceptions import MaxTokensReachedException

    try:
        return str(agent(message))
    except MaxTokensReachedException:
        trace.event("max_tokens", detail="a response exceeded max_tokens; the run stopped here")
        return "[stopped: a model response exceeded max_tokens]"


def _repl(agent: Any, env: Env, trace_path: Path) -> str:
    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return "quit"
        if not line:
            continue
        if line in ("/quit", "/exit"):
            return "quit"
        if line == "/reset":
            return "reset"
        if line == "/help":
            print(HELP)
        elif line == "/tools":
            print("\n".join(f"  - {name}" for name in getattr(agent, "tool_names", [])))
        elif line == "/stats":
            print(json.dumps(trace.totals(), indent=2))
        elif line == "/state":
            print(json.dumps(env.describe(), indent=2, ensure_ascii=False))
        elif line == "/trace":
            lines = trace_path.read_text(encoding="utf-8").splitlines()[-10:] if trace_path.exists() else []
            print("\n".join("  " + raw[:300] for raw in lines))
        else:
            message = env.instruction if line == "/task" else line
            if line == "/task":
                print(f"task> {message}")
            try:
                _ask(agent, message)
            except KeyboardInterrupt:
                print("\n(interrupted)")


def _print_summary(env: Env) -> None:
    print("stats:", json.dumps(trace.totals()))
    print("state:", json.dumps(env.describe(), ensure_ascii=False))
