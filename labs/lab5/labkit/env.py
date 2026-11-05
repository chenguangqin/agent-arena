"""The task environment: the online store's operations API, served over MCP.

* Locally (chat, ``-p``), ``Env.local()`` starts the scenario on 127.0.0.1.
* In the Arena, ``Env.from_protocol()`` connects to the gateway announced in
  the environment variables.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

from labkit import scenario


class Env:
    def __init__(self, url: str, headers: dict[str, str], instruction: str, server: Any = None) -> None:
        self.url = url
        self.headers = headers
        self.instruction = instruction
        self._server = server
        self._client: Any = None

    # ---- construction ------------------------------------------------------------
    @classmethod
    def local(cls, seed: int = 0) -> "Env":
        from labkit.server import DevStoreServer

        state = scenario.build(seed)
        server = DevStoreServer(state, name="store-raw-api")
        server.start()
        env = cls(server.url or "", server.headers, scenario.INSTRUCTION, server)
        env.state = state
        return env

    @classmethod
    def from_protocol(cls, instruction: str) -> "Env":
        headers = json.loads(os.environ.get("AGENT_EVAL_MCP_HEADERS") or "{}")
        return cls(os.environ["AGENT_EVAL_MCP_URL"], headers, instruction)

    # ---- what agent.py uses ----------------------------------------------------------
    def mcp_client(self, **kwargs: Any) -> Any:
        """A Strands MCPClient for the environment. Put it straight into Agent(tools=[...]).

        kwargs are passed to MCPClient, e.g. tool_filters={"allowed": ["get_order", "replay_order"]}
        to give a workflow node only the tools it needs.
        """
        from strands.tools.mcp import MCPClient

        return MCPClient(url=self.url, headers=dict(self.headers), **kwargs)

    def tool_catalog(self) -> list[dict[str, str]]:
        """Name and description of every environment tool (e.g. to show an orchestrator)."""
        return [{"name": t.tool_name, "description": t.tool_spec.get("description", "")}
                for t in self._ensure_client().list_tools_sync()]

    def call(self, name: str, **arguments: Any) -> Any:
        """Call one environment tool from Python code (e.g. for deterministic checks).

        Returns the decoded JSON result; raises RuntimeError with the tool's error text.
        """
        result = self._ensure_client().call_tool_sync(f"env-{uuid.uuid4().hex[:8]}", name, arguments)
        text = "\n".join(block.get("text", "") for block in result.get("content", []))
        if result.get("status") == "error":
            raise RuntimeError(text)
        structured = result.get("structuredContent")
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        if structured is not None:
            return structured
        return json.loads(text)

    def _ensure_client(self) -> Any:
        if self._client is None:
            from strands.tools.mcp import MCPClient

            self._client = MCPClient(url=self.url, headers=dict(self.headers))
            self._client.start()
        return self._client

    # ---- lab-only helpers --------------------------------------------------------------
    def describe(self) -> dict[str, Any] | None:
        """Final state of the local scenario (not available in the Arena)."""
        state = getattr(self, "state", None)
        return scenario.describe(state) if state is not None else None

    def close(self) -> None:
        if self._client is not None:
            self._client.stop(None, None, None)
            self._client = None
        if self._server is not None:
            self._server.stop()
            self._server = None
