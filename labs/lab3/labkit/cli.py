"""Command line for agent.py: interactive chat, one-shot prompt, or the scenario task.

    uv run python agent.py                       # chat
    uv run python agent.py -p "问题"             # one question, then exit
    uv run python agent.py --task                # run the scenario task once and show the result
    uv run python agent.py --mode offload ...    # select a mode your build_agent() understands

Built-in chat commands are kept to a minimum (/task, /state, /help, /reset, /quit).
Every other line starting with "/" is handed to ``handle_command(agent, line)`` from
agent.py — that is where you add /context, /compact, ... (goal 1).
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

BuildAgent = Callable[[Env, str], Any]
HandleCommand = Callable[[Any, str], bool]
HELP = """built-in commands:
  /task     send the scenario task
  /state    what actually happened in the environment
  /help     this help (then your own commands)
  /reset    fresh agent and fresh environment
  /quit     exit"""


def run_cli(build_agent: BuildAgent, modes: list[str], handle_command: HandleCommand | None = None,
            argv: list[str] | None = None) -> int:
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
                reply = _ask(agent, env.instruction if args.task else args.prompt)
                print("stats:", json.dumps(trace.totals()))
                print("state:", json.dumps(env.describe(reply), ensure_ascii=False))
                return 0
            print(f"mode={args.mode} · trace → {trace_path}\n{HELP}\n")
            if repl(agent, env, handle_command) == "quit":
                return 0
            print("\n--- reset ---\n")
        finally:
            agent.cleanup()
            env.close()


def _build(build_agent: BuildAgent, env: Env, mode: str) -> Any:
    agent = build_agent(env, mode)
    agent.hooks.add_hook(trace.Tracer())
    trace.reset_totals()
    return agent


def _ask(agent: Any, message: str) -> str:
    reply = str(agent(message))
    print(f"\nagent> {reply}\n")
    return reply


def dispatch(agent: Any, env: Env, line: str, handle_command: HandleCommand | None) -> str | None:
    """Handle one chat line. Returns "quit"/"reset" to end the session, otherwise None."""
    if line in ("/quit", "/exit"):
        return "quit"
    if line == "/reset":
        return "reset"
    if line == "/help":
        print(HELP)
        if handle_command is not None:
            handle_command(agent, "/help")
        return None
    if line == "/state":
        print(json.dumps(env.describe(), indent=2, ensure_ascii=False))
        return None
    if line == "/task":
        print(f"task> {env.instruction}")
        _ask(agent, env.instruction)
        return None
    if line.startswith("/"):
        if handle_command is None or not handle_command(agent, line):
            print(f"unknown command: {line.split()[0]} (see /help)")
        return None
    _ask(agent, line)
    return None


def repl(agent: Any, env: Env, handle_command: HandleCommand | None) -> str:
    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return "quit"
        if not line:
            continue
        try:
            outcome = dispatch(agent, env, line, handle_command)
        except KeyboardInterrupt:
            print("\n(interrupted)")
            continue
        if outcome:
            return outcome
