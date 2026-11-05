"""Lab1 · M1 demo: the plain Strands agent loop with no harness features."""

from __future__ import annotations

from pathlib import Path

from core.config import Config
from core.lab import Lab

PROMPT = (
    "You are an agent operating in an isolated task environment. "
    "Use the available tools to inspect the environment and complete the task. "
    "Act instead of merely explaining."
)


def system_prompt(config: Config) -> str:
    return PROMPT


LAB = Lab(
    name="lab1",
    title="Lab1 · Agent loop (M1)",
    root=Path(__file__).resolve().parent,
    schema={},
    scenario="intro-refund",
    system_prompt=system_prompt,
)
