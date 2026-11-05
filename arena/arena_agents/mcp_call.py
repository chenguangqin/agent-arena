#!/usr/bin/env python3
"""Runs inside the task container: call one MCP tool and print the result as JSON.

    python3 mcp_call.py <mcp-url> <tool-name> <base64-json-arguments>

The host-side agent invokes this through Harbor's ``environment.exec`` because the
task's MCP server is only reachable on the task's private Docker network.
Output (last stdout line): {"is_error": bool, "text": str}
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys


async def _call(url: str, tool: str, arguments: dict) -> dict:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(url) as (read, write, _), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool(tool, arguments)
    parts = [c.text for c in result.content if getattr(c, "type", "") == "text"]
    text = "\n".join(parts)
    if not text and getattr(result, "structuredContent", None) is not None:
        text = json.dumps(result.structuredContent, ensure_ascii=False)
    return {"is_error": bool(result.isError), "text": text}


def main() -> int:
    url, tool = sys.argv[1], sys.argv[2]
    raw = sys.argv[3] if len(sys.argv) > 3 else ""
    arguments = json.loads(base64.b64decode(raw)) if raw else {}
    try:
        out = asyncio.run(_call(url, tool, arguments))
    except BaseException as exc:  # noqa: BLE001 - report every failure to the host
        detail = exc
        while isinstance(detail, BaseExceptionGroup) and detail.exceptions:
            detail = detail.exceptions[0]
        out = {"is_error": True, "text": f"{type(detail).__name__}: {detail}"}
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
