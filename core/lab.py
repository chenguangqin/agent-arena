"""Lab definition and agent assembly.

A lab declares its feature schema, profiles, system prompt, default scenario,
and an ordered list of features. Each feature's ``install(spec, ctx)`` adds
tools, hooks, plugins, or a conversation manager to an ``AgentSpec`` before the
Strands ``Agent`` is created, so a Lab only touches its own feature files.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.budget import Budget
from core.config import Config, Schema
from core.model import build_model, build_retry_strategy
from core.trace import AgentTracer, Trace


@dataclass
class RunContext:
    """Per-run state shared by every feature and helper agent."""

    config: Config
    agent_root: Path
    instruction: str
    trace: Trace
    budget: Budget
    env_tools: list[Any] = field(default_factory=list)  # tools served by the environment (MCP)
    env_client: Any | None = None  # MCPClient, for harness code that queries the environment
    env_url: str = ""  # MCP gateway, for features that start their own MCP clients or servers
    env_headers: dict[str, str] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    model_factory: Callable[[str | None], Any] | None = None  # injectable for tests
    # Resources features open for the run (e.g. a stdio MCP server); closed when the run ends.
    resources: contextlib.ExitStack = field(default_factory=contextlib.ExitStack)

    @property
    def reports_dir(self) -> Path:
        return self.agent_root / "reports"

    def env_tool(self, name: str) -> Any:
        for tool in self.env_tools:
            if tool.tool_name == name:
                return tool
        raise KeyError(f"environment has no tool {name!r}")

    def call_env(self, name: str, **arguments: Any) -> Any:
        """Call an environment tool directly from harness code (not visible to the model)."""
        if self.env_client is None:
            raise RuntimeError("no environment client")
        result = self.env_client.call_tool_sync(f"harness-{uuid.uuid4().hex[:8]}", name, arguments)
        text = "\n".join(b.get("text", "") for b in result.get("content", []))
        if result.get("status") == "error":
            raise RuntimeError(text)
        structured = result.get("structuredContent")
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        if structured is not None:
            return structured
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def model(self, model_id: str | None = None, cache: bool = False) -> Any:
        if self.model_factory is not None:
            return self.model_factory(model_id)
        return build_model(self.config, model_id=model_id, cache=cache)


@dataclass
class AgentSpec:
    """Constructor arguments that features extend before the Agent is built."""

    system_prompt: str
    tools: list[Any]
    hooks: list[Any] = field(default_factory=list)
    plugins: list[Any] = field(default_factory=list)
    interventions: list[Any] = field(default_factory=list)
    conversation_manager: Any | None = None
    tool_executor: Any | None = None
    cache: bool = False
    state: dict[str, Any] = field(default_factory=dict)
    # Functions applied to the instruction before the first invocation.
    instruction_transforms: list[Callable[[str], str]] = field(default_factory=list)


@dataclass(frozen=True)
class Feature:
    section: str
    key: str
    install: Callable[[AgentSpec, RunContext], None]
    title: str = ""


Driver = Callable[["Lab", RunContext], Any]


@dataclass(frozen=True)
class Lab:
    name: str
    title: str
    root: Path
    schema: Schema
    scenario: str
    system_prompt: Callable[[Config], str]
    features: tuple[Feature, ...] = ()
    # Optional custom driver (Lab4 runs one fresh agent per ticket). Returns an AgentResult.
    driver: Driver | None = None
    # Directories under the lab that persist across tasks (Command Adapter snapshot_paths).
    state_paths: tuple[str, ...] = ()

    def state_dir(self, ctx: "RunContext", rel: str) -> Path:
        """Per-run copy of a state directory, e.g. ``lab.state_dir(ctx, "memory")``."""
        return ctx.agent_root / self.name / rel

    @property
    def profiles_dir(self) -> Path:
        return self.root / "profiles"


def build_agent(lab: Lab, ctx: RunContext, *, label: str = "main") -> tuple[Any, AgentSpec]:
    """Create the lab's agent with every enabled feature installed."""
    from strands import Agent

    tracer = AgentTracer(ctx.trace, label)
    spec = AgentSpec(system_prompt=lab.system_prompt(ctx.config), tools=list(ctx.env_tools), hooks=[tracer])
    enabled = []
    for feature in lab.features:
        if ctx.config.enabled(feature.section, feature.key):
            feature.install(spec, ctx)
            enabled.append(f"{feature.section}.{feature.key}")
    ctx.summary["features"] = enabled

    kwargs: dict[str, Any] = {
        "model": tracer.tap(ctx.model(cache=spec.cache)),
        "tools": spec.tools,
        "system_prompt": spec.system_prompt,
        "hooks": spec.hooks,
        "plugins": spec.plugins,
        "interventions": spec.interventions,
        "callback_handler": None,
        "name": f"{lab.name}-{label}",
        "retry_strategy": build_retry_strategy(),
        "state": dict(spec.state),
    }
    if spec.conversation_manager is not None:
        kwargs["conversation_manager"] = spec.conversation_manager
    if spec.tool_executor is not None:
        kwargs["tool_executor"] = spec.tool_executor
    return Agent(**kwargs), spec


def helper_agent(
    ctx: RunContext,
    *,
    label: str,
    system_prompt: str,
    tools: list[Any] | None = None,
    model_id: str | None = None,
    hooks: list[Any] | None = None,
    **kwargs: Any,
) -> Any:
    """A traced, budget-counted agent with its own fresh context (judge, verifier, worker)."""
    from strands import Agent

    tracer = AgentTracer(ctx.trace, label)
    return Agent(
        model=tracer.tap(ctx.model(model_id)),
        tools=list(tools or []),
        system_prompt=system_prompt,
        hooks=[tracer, *(hooks or [])],
        callback_handler=None,
        name=label,
        retry_strategy=build_retry_strategy(),
        **kwargs,
    )


def default_driver(lab: Lab, ctx: RunContext) -> Any:
    from strands.types.agent import Limits

    agent, spec = build_agent(lab, ctx)
    instruction = ctx.instruction
    for transform in spec.instruction_transforms:
        instruction = transform(instruction)
    return agent(instruction, limits=Limits(turns=ctx.config.get("limits", "max_turns")))
