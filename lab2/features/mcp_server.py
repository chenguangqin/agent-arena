"""L2.5 · Connect to the self-built facade MCP server instead of the raw API.

The agent starts ``python -m lab2.facade`` over stdio, passing the gateway
address, and uses only the facade's tools. Compare with L2.1–L2.4, which do
the same work as wrappers and hooks inside this agent.
"""

from __future__ import annotations

import json
import os
import sys

from core.lab import AgentSpec, RunContext
from lab2.features.common import ALIASES

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def install(spec: AgentSpec, ctx: RunContext) -> None:
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from strands.tools.mcp import MCPClient

    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG") or k.startswith("AWS_")}
    env.update(AGENT_EVAL_MCP_URL=ctx.env_url, AGENT_EVAL_MCP_HEADERS=json.dumps(ctx.env_headers),
               PYTHONPATH=REPO)
    params = StdioServerParameters(command=sys.executable, args=["-m", "lab2.facade"], env=env)
    client = ctx.resources.enter_context(MCPClient(lambda: stdio_client(params)))
    tools = client.list_tools_sync()
    ALIASES.update(refund_payment="refunds_create_v2", request_refund_approval="approvals_request")
    ctx.trace.feature("mcp_server", server="lab2.facade", raw_tools=len(spec.tools),
                      facade_tools=[t.tool_name for t in tools])
    spec.tools[:] = tools
