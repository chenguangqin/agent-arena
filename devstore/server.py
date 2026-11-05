"""Loopback-only MCP server exposing a scenario's store as tools.

It stands in for the Arena MCP Gateway during local development, so it follows
the same contract: Streamable HTTP on 127.0.0.1, a random port, and a per-run
bearer token that the agent receives through ``AGENT_EVAL_MCP_HEADERS``.
There is intentionally no option to bind another interface.
"""

from __future__ import annotations

import asyncio
import functools
import secrets
import socket
import threading
import time
from contextlib import contextmanager
from collections.abc import Callable, Generator
from typing import Any

import uvicorn
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


LOOPBACK = "127.0.0.1"


ToolDef = tuple[str, str, Callable[..., Any]]  # (name, description, function)


def build_mcp(name: str, tools: list[ToolDef], calls: list[dict[str, Any]]) -> MCPServer:
    """Register scenario tools; every call is appended to ``calls`` for inspection."""
    mcp = MCPServer(
        name,
        instructions="Online store operations for the current scenario. Data is synthetic.",
        log_level="ERROR",
    )
    for tool_name, description, fn in tools:
        mcp.tool(name=tool_name, description=description)(_recorded(tool_name, fn, calls))
    return mcp


def _recorded(name: str, fn: Callable[..., Any], calls: list[dict[str, Any]]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        entry: dict[str, Any] = {"ts": time.time(), "name": name, "args": kwargs}
        try:
            result = fn(*args, **kwargs)
            entry["ok"] = True
            return result
        except Exception as exc:
            entry.update(ok=False, error=str(exc))
            # MCPServer hides the message of unexpected exceptions; ToolError passes it
            # through, so the model sees "503 ..." vs "OUT_OF_STOCK ..." like a real API.
            raise ToolError(f"{type(exc).__name__}: {exc}") from exc
        finally:
            calls.append(entry)

    return wrapper


class _BearerToken:
    """Reject every HTTP request that does not carry the run's bearer token."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            supplied = dict(scope.get("headers", [])).get(b"authorization", b"")
            if not secrets.compare_digest(supplied, self.expected):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


class _Server(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


class DevStoreServer:
    """Serve a scenario environment over MCP in a background thread; use as a context manager.

    ``env`` is any object with a ``tools() -> list[ToolDef]`` method.
    """

    def __init__(self, env: Any, name: str = "devstore") -> None:
        self.env = env
        self.name = name
        self.calls: list[dict[str, Any]] = []
        self.token = secrets.token_urlsafe(32)
        self.url: str | None = None
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((LOOPBACK, 0))
        port = sock.getsockname()[1]
        app = build_mcp(self.name, self.env.tools(), self.calls).streamable_http_app(
            streamable_http_path="/mcp", json_response=True, stateless_http=True, host=LOOPBACK
        )
        config = uvicorn.Config(
            _BearerToken(app, self.token), host=LOOPBACK, port=port, log_level="error", access_log=False
        )
        self._server = _Server(config)
        self._thread = threading.Thread(
            target=lambda: asyncio.run(self._server.serve(sockets=[sock])), daemon=True
        )
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline or not self._thread.is_alive():
                raise RuntimeError("devstore MCP server failed to start")
            time.sleep(0.02)
        self.url = f"http://{LOOPBACK}:{port}/mcp"

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = self._thread = None
        self.url = None

    def __enter__(self) -> "DevStoreServer":
        self.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.stop()
