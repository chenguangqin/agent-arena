"""The task environment: the incident (logs, runbooks, configs), served over MCP.

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
        server = DevStoreServer(state, name="incident-env")
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
        """A Strands MCPClient for the environment tools. Put it straight into Agent(tools=[...]).

        kwargs are passed to MCPClient, e.g. tool_filters={"rejected": ["read_logs"]}.
        """
        from strands.tools.mcp import MCPClient

        return MCPClient(url=self.url, headers=dict(self.headers), **kwargs)

    def call(self, name: str, **arguments: Any) -> Any:
        """Call one environment tool from Python code (for building your own tools).

        Returns the decoded JSON result; raises RuntimeError with the API's error text.
        """
        if self._client is None:
            from strands.tools.mcp import MCPClient

            self._client = MCPClient(url=self.url, headers=dict(self.headers))
            self._client.start()
        result = self._client.call_tool_sync(f"env-{uuid.uuid4().hex[:8]}", name, arguments)
        text = "\n".join(block.get("text", "") for block in result.get("content", []))
        if result.get("status") == "error":
            raise RuntimeError(text)
        structured = result.get("structuredContent")
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        if structured is not None:
            return structured
        return json.loads(text)

    # ---- lab-only helpers --------------------------------------------------------------
    def describe(self, final_text: str | None = None) -> dict[str, Any] | None:
        """Final state of the local scenario (not available in the Arena)."""
        state = getattr(self, "state", None)
        return scenario.describe(state, final_text) if state is not None else None

    def close(self) -> None:
        if self._client is not None:
            self._client.stop(None, None, None)
            self._client = None
        if self._server is not None:
            self._server.stop()
            self._server = None
