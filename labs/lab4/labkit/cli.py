"""Command line for agent.py: the ticket queue, one fresh session per ticket.

    uv run python agent.py --task                 # work the whole queue (10 tickets), then show results
    uv run python agent.py --task --reset-memory  # same, starting from an empty memory/ directory
    uv run python agent.py                        # chat: /next handles the next ticket in a new session
    uv run python agent.py --mode full ...        # a mode your build_agent() understands
"""

from __future__ import annotations

import argparse
import json
import time
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from labkit import trace
from labkit.env import Env
from labkit.session import Session, collect_trajectory

BuildAgent = Callable[[Env, str, Session], Any]
OnSessionEnd = Callable[[Env, str, Session], None]
MAX_TICKETS = 20


@dataclass
class CommandContext:
    """Passed to your slash commands."""

    env: Env
    mode: str
    memory_dir: Path
    skills_dir: Path
    last_session: Session | None


Command = Callable[[str, CommandContext], None]  # (argument text, context)

HELP = """commands:
  /next     handle the next ticket in a NEW session (fresh agent, empty context)
  /queue    tickets handled so far and what is next
  /state    per-ticket outcome against the policies (what actually happened)
  /stats    tokens and tool calls
  /trace    last 10 trace events
  /reset    fresh support desk (memory/ is kept)
  /quit     exit
anything else is sent to a free-chat agent built with the current memory (no ticket)"""


def run_ticket(env: Env, build_agent: BuildAgent, on_session_end: OnSessionEnd, mode: str,
               memory_dir: Path, skills_dir: Path, index: int, reports: Path | None = None) -> Session | None:
    """Handle the next ticket in a fresh session. Returns None when the queue is empty."""
    from strands.types.exceptions import MaxTokensReachedException

    ticket = env.call("next_ticket")
    if ticket.get("done"):
        return None
    session = Session(index=index, ticket=ticket, memory_dir=memory_dir, skills_dir=skills_dir)
    trace.event("session_start", ticket=ticket["id"], index=index)
    agent = build_agent(env, mode, session)
    agent.hooks.add_hook(trace.Tracer(label=ticket["id"]))
    try:
        agent(session.prompt)
    except MaxTokensReachedException:
        trace.event("max_tokens", ticket=ticket["id"])
    finally:
        traj = collect_trajectory(agent, ticket["id"])
        agent.cleanup()
    after = env.call("next_ticket")
    traj.submitted = after.get("id") != ticket["id"]
    if not traj.submitted:
        env.call("skip_ticket", ticket_id=ticket["id"], reason="agent ended without submitting")
    traj.feedback = env.call("get_feedback", ticket_id=ticket["id"]).get("feedback", "")
    session.trajectory = traj
    if reports is not None:
        reports.mkdir(parents=True, exist_ok=True)
        (reports / f"{ticket['id']}.json").write_text(json.dumps(traj.to_dict(), ensure_ascii=False, indent=2),
                                                      encoding="utf-8")
    on_session_end(env, mode, session)
    trace.event("session_end", ticket=ticket["id"], submitted=traj.submitted, tool_calls=len(traj.tool_calls))
    return session


def run_queue(env: Env, build_agent: BuildAgent, on_session_end: OnSessionEnd, mode: str,
              memory_dir: Path, skills_dir: Path, reports: Path | None = None) -> list[Session]:
    sessions = []
    for index in range(1, MAX_TICKETS + 1):
        session = run_ticket(env, build_agent, on_session_end, mode, memory_dir, skills_dir, index, reports)
        if session is None:
            break
        sessions.append(session)
    return sessions


def reset_memory(memory_dir: Path) -> None:
    """Empty memory_dir (keeps the directory and .gitkeep)."""
    memory_dir.mkdir(parents=True, exist_ok=True)
    for child in memory_dir.iterdir():
        if child.name == ".gitkeep":
            continue
        shutil.rmtree(child) if child.is_dir() else child.unlink()


def run_cli(build_agent: BuildAgent, on_session_end: OnSessionEnd, modes: list[str],
            commands: dict[str, Command] | None = None, argv: list[str] | None = None,
            skills_dir: Path = Path("skills")) -> int:
    parser = argparse.ArgumentParser(description="Lab4 agent")
    parser.add_argument("--mode", default=modes[0], choices=modes)
    parser.add_argument("--task", action="store_true", help="work the whole queue and exit")
    parser.add_argument("--memory-dir", type=Path, default=Path("memory"))
    parser.add_argument("--reset-memory", action="store_true", help="start from an empty memory directory")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    commands = dict(commands or {})

    # One file per run, so parallel or repeated runs never mix their events.
    trace_path = Path("reports") / f"trace-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    trace.configure(trace_path, echo=True)
    if args.reset_memory:
        reset_memory(args.memory_dir)
    args.memory_dir.mkdir(parents=True, exist_ok=True)

    while True:
        env = Env.local(args.seed)
        try:
            if args.task:
                run_queue(env, build_agent, on_session_end, args.mode, args.memory_dir, skills_dir,
                          Path("reports") / "sessions")
                _print_summary(env)
                return 0
            print(f"mode={args.mode} · memory → {args.memory_dir}/ · trace → {trace_path}\n{HELP}")
            if commands:
                print("your commands: " + ", ".join(sorted(commands)))
            print()
            outcome = _repl(env, build_agent, on_session_end, args, commands, skills_dir)
            if outcome == "quit":
                return 0
            print("\n--- reset ---\n")
        finally:
            env.close()


def _repl(env: Env, build_agent: BuildAgent, on_session_end: OnSessionEnd, args: Any,
          commands: dict[str, Command], skills_dir: Path) -> str:
    sessions: list[Session] = []
    chat_agent: Any = None
    try:
        while True:
            try:
                line = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return "quit"
            if not line:
                continue
            name, _, rest = line.partition(" ")
            if name in ("/quit", "/exit"):
                return "quit"
            if name == "/reset":
                return "reset"
            if name == "/help":
                print(HELP)
            elif name == "/next":
                session = run_ticket(env, build_agent, on_session_end, args.mode, args.memory_dir, skills_dir,
                                     len(sessions) + 1, Path("reports") / "sessions")
                if session is None:
                    print("队列已空。")
                else:
                    sessions.append(session)
                    print(f"\n{session.ticket['id']} → submitted={session.trajectory.submitted}"
                          f"\nagent> {session.trajectory.reply}\nfeedback> {session.trajectory.feedback}\n")
                if chat_agent is not None:  # rebuild so free chat sees the updated memory
                    chat_agent.cleanup()
                    chat_agent = None
            elif name == "/queue":
                done = [f"{s.ticket['id']}({'ok' if s.trajectory.submitted else 'skipped'})" for s in sessions]
                print("handled:", ", ".join(done) or "-", "| next:", env.call("next_ticket").get("id", "队列已空"))
            elif name == "/state":
                print(json.dumps(env.describe(), indent=2, ensure_ascii=False))
            elif name == "/stats":
                print(json.dumps(trace.totals(), indent=2))
            elif name == "/trace":
                path = trace.current_path() or Path("reports") / "missing.jsonl"
                lines = path.read_text(encoding="utf-8").splitlines()[-10:] if path.exists() else []
                print("\n".join("  " + raw[:300] for raw in lines))
            elif name in commands:
                commands[name](rest.strip(), CommandContext(env, args.mode, args.memory_dir, skills_dir,
                                                            sessions[-1] if sessions else None))
            elif name.startswith("/"):
                print(f"unknown command {name}; /help lists commands")
            else:
                if chat_agent is None:
                    chat_agent = build_agent(env, args.mode, Session(0, None, args.memory_dir, skills_dir))
                    chat_agent.hooks.add_hook(trace.Tracer(label="chat"))
                try:
                    print(f"\nagent> {chat_agent(line)}\n")
                except KeyboardInterrupt:
                    print("\n(interrupted)")
    finally:
        if chat_agent is not None:
            chat_agent.cleanup()


def _print_summary(env: Env) -> None:
    state = env.describe() or {}
    print("\n=== per ticket ===")
    for row in state.get("tickets", []):
        checks = {k: v for k, v in row.items() if k not in ("ticket", "status", "steps", "policies_read")}
        print(f"{row['ticket']} {row['status']:<8} steps={row['steps']:<3} policies={row['policies_read']} {checks}")
    print("stats:", json.dumps(trace.totals()))
    print("state:", json.dumps({k: v for k, v in state.items() if k != "tickets"}, ensure_ascii=False))
