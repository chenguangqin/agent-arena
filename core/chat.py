"""Interactive chat for classroom demos.

Starts the lab's devstore scenario in-process, connects over MCP exactly as in
protocol mode, and prints tool calls and feature events as they happen.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from core.config import load_profile
from core.lab import Lab, build_agent
from core.protocol import final_text, new_context

HELP = """commands:
  /task          send the scenario's task instruction
  /tools         list the tools the model sees
  /trace [n]     show the last n trace events (default 10)
  /stats         tokens, peak context, model and tool calls, feature events
  /reset         new conversation and a fresh environment
  /quit          exit
anything else is sent to the agent as a user message"""


def prepare_agent_root(lab: Lab, base: Path) -> Path:
    """Mirror the evaluation harness: copies of the lab's state directories."""
    root = base / "agent"
    for rel in lab.state_paths:
        source = lab.root / rel
        target = root / lab.name / rel
        if source.exists():
            shutil.copytree(source, target)
        else:
            target.mkdir(parents=True)
    for name in ("reports", "run"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


class ConsolePrinter:
    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose

    def __call__(self, event: dict[str, Any]) -> None:
        kind = event["event"]
        agent = event.get("agent", "main")
        tag = "" if agent == "main" else f"[{agent}] "
        if kind == "tool_call":
            status = "✗" if event.get("status") == "error" else "✓"
            note = f" (blocked: {event['cancelled']})" if event.get("cancelled") else ""
            print(f"  {tag}{status} {event['name']}({_short(event.get('input'), 120)})"
                  f" → {event.get('result_chars', 0)} chars{note}")
            if self.verbose:
                print(f"      {_short(event.get('result'), 300)}")
        elif kind == "model_call":
            print(f"  {tag}· model call #{event['index']}: context {event.get('context_tokens')} tokens,"
                  f" stop={event.get('stop_reason')}")
        elif kind == "feature":
            fields = {k: v for k, v in event.items() if k not in ("ts", "event", "name")}
            print(f"  {tag}◆ {event['name']}: {_short(fields, 240)}")


def _short(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def run_chat(lab: Lab, profile: str, scenario_id: str, seed: int, verbose: bool) -> None:
    from strands.tools.mcp import MCPClient
    from strands.types.agent import Limits

    from devstore import scenarios
    from devstore.server import DevStoreServer

    scenario = scenarios.get(scenario_id)
    config = load_profile(profile, lab.profiles_dir, lab.schema)
    limits = Limits(turns=config.get("limits", "max_turns"))
    print(f"{lab.title} · profile={profile} · scenario={scenario.ID} · model={config.get('model', 'id')}")
    print(HELP)

    while True:  # one iteration per /reset
        base = Path(tempfile.mkdtemp(prefix=f"{lab.name}-chat-"))
        root = prepare_agent_root(lab, base)
        env = scenario.build(seed)
        with DevStoreServer(env) as server:
            client = MCPClient(url=server.url, headers=server.headers)
            with client:
                ctx = new_context(lab, config, root, scenario.INSTRUCTION,
                                  trace_path=root / "reports" / "trace.jsonl")
                ctx.env_tools = client.list_tools_sync()
                ctx.env_client = client
                ctx.env_url, ctx.env_headers = server.url or "", server.headers
                ctx.trace.subscribe(ConsolePrinter(verbose))
                with ctx.resources:
                    agent, spec = build_agent(lab, ctx)
                    print(f"(agent_root: {root})\n")
                    outcome = _loop(lab, ctx, agent, spec, limits)
                if outcome == "quit":
                    return
        print("\n--- reset ---\n")


def _loop(lab: Lab, ctx: Any, agent: Any, spec: Any, limits: Any) -> str:
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
            for name in agent.tool_names:
                print(f"  - {name}")
        elif line.startswith("/trace"):
            _show_trace(ctx, line)
        elif line == "/stats":
            print(json.dumps({**ctx.budget.as_dict(), "tool_calls": ctx.trace.tool_calls,
                              "events": dict(ctx.trace.counts)}, indent=2, ensure_ascii=False))
        else:
            message = line
            if line == "/task":
                print(f"task> {ctx.instruction}")
                if lab.driver is not None:  # e.g. Lab4 runs one fresh agent per ticket
                    print(f"agent> {final_text(lab.driver(lab, ctx))}\n")
                    continue
                message = ctx.instruction
                for transform in spec.instruction_transforms:
                    message = transform(message)
            try:
                result = agent(message, limits=limits)
            except KeyboardInterrupt:
                print("\n(interrupted)")
                continue
            print(f"agent> {final_text(result)}\n")


def _show_trace(ctx: Any, line: str) -> None:
    parts = line.split()
    count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
    path = ctx.trace.path
    if path is None or not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines()[-count:]:
        print("  " + _short(json.loads(raw), 300))
