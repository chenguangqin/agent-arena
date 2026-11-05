"""目标 1 · slash command 机制，以及 /context 和 /compact 的入口。

一个命令 = 名字 + 说明 + 处理函数。用 @command 注册；handle_command() 按名字分发，
labkit 的 chat 会把所有非内置的 "/..." 输入交给它。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from labkit import trace
from solution.tokens import estimate

CONTEXT_WINDOW = 200_000  # Claude Haiku 4.5 on Bedrock


@dataclass
class Command:
    name: str
    help: str
    run: Callable[[Any, str], None]  # (agent, argument text)


COMMANDS: dict[str, Command] = {}


def command(name: str, help: str) -> Callable:
    def register(fn: Callable[[Any, str], None]) -> Callable[[Any, str], None]:
        COMMANDS[name] = Command(name, help, fn)
        return fn
    return register


def handle_command(agent: Any, line: str) -> bool:
    name, _, argument = line.strip().partition(" ")
    if name == "/help":
        for cmd in COMMANDS.values():
            print(f"  {cmd.name:<10}{cmd.help}")
        return True
    cmd = COMMANDS.get(name)
    if cmd is None:
        return False
    cmd.run(agent, argument.strip())
    return True


# ---- /context ---------------------------------------------------------------------------

def context_breakdown(agent: Any) -> dict[str, int]:
    """Estimated tokens per part of what the next model call will send."""
    parts = {"system_prompt": estimate(agent.system_prompt or ""),
             "tool_definitions": estimate(agent.tool_registry.get_all_tool_specs()),
             "user_text": 0, "assistant_text": 0, "tool_calls": 0, "tool_results": 0}
    for message in agent.messages:
        for block in message.get("content", []):
            if "toolResult" in block:
                parts["tool_results"] += estimate(block["toolResult"].get("content", []))
            elif "toolUse" in block:
                parts["tool_calls"] += estimate(block["toolUse"])
            elif "text" in block:
                parts["user_text" if message["role"] == "user" else "assistant_text"] += estimate(block["text"])
    return parts


@command("/context", "显示上下文的构成（估算 token）以及上一次调用的实际输入 token")
def show_context(agent: Any, _: str) -> None:
    parts = context_breakdown(agent)
    total = sum(parts.values())
    print(f"  messages: {len(agent.messages)}   estimated total: {total} tokens "
          f"({total / CONTEXT_WINDOW:.1%} of {CONTEXT_WINDOW})")
    for name, tokens in parts.items():
        bar = "█" * round(30 * tokens / max(total, 1))
        print(f"  {name:<17}{tokens:>8}  {tokens / max(total, 1):>6.1%}  {bar}")
    usage = trace.last_usage()
    if usage:
        actual = usage.get("input", 0) + usage.get("cache_read", 0) + usage.get("cache_write", 0)
        print(f"  last model call actually sent: {actual} input tokens")
    trace.event("context", total_estimate=total, **parts)


# ---- /compact ------------------------------------------------------------------------------

@command("/compact", "压缩对话：/compact [压缩时要保留的重点]")
def compact_now(agent: Any, focus: str) -> None:
    from solution.compaction import Compactor

    compactor = agent.conversation_manager if isinstance(agent.conversation_manager, Compactor) else Compactor()
    if not compactor.compact(agent, focus=focus or None, reason="manual"):
        print("  nothing to compact yet")
