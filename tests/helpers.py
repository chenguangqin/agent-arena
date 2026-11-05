"""Run a lab with an ad-hoc profile against its real scenario, with scripted models."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from core.config import load_profile
from core.lab import Lab
from core.protocol import new_context
from devstore.server import DevStoreServer


@contextmanager
def lab_run(lab: Lab, scenario: Any, tmp_path, profile_body: str, models: list) -> Iterator:
    """Yields (lab, ctx, env). Models are handed out in the order agents are created."""
    from strands.tools.mcp import MCPClient

    profiles = tmp_path / "lab" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "test.toml").write_text(profile_body, encoding="utf-8")
    lab = dataclasses.replace(lab, root=tmp_path / "lab")
    config = load_profile("test", lab.profiles_dir, lab.schema)
    env = scenario.build()
    queue = list(models)
    with DevStoreServer(env) as server, MCPClient(url=server.url, headers=server.headers) as client:
        ctx = new_context(lab, config, tmp_path / "agent", scenario.INSTRUCTION,
                          trace_path=tmp_path / "trace.jsonl", model_factory=lambda _id: queue.pop(0))
        ctx.env_tools = client.list_tools_sync()
        ctx.env_client = client
        ctx.env_url, ctx.env_headers = server.url, server.headers
        with ctx.resources:
            yield lab, ctx, env


def features(ctx, name: str) -> list[dict]:
    lines = ctx.trace.path.read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e["event"] == "feature" and e["name"] == name]


def tool_events(ctx) -> list[dict]:
    lines = ctx.trace.path.read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e["event"] == "tool_call"]
