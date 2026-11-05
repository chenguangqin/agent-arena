"""Lab2 参考答案。

    uv run python -m solution.agent --mode full --task
    uv run python -m solution.agent --mode mcp           # 对话

模式：
    baseline    原始 API 的全部接口
    tool        目标 1：自定义工具 find_duplicate_charges + 隐藏不该用的接口
    mcp         目标 2：只连接自己写的 MCP server（solution/mcp_server.py）
    code        目标 3：原始只读接口 + 沙箱代码执行工具 run_python
    governance  目标 4：原始 API + 治理 hook
    full        目标 1 + 目标 4
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from strands import Agent

from labkit import Env, build_model, run_cli
from solution.code_exec import make_run_python
from solution.governance import Governance
from solution.tools import HIDDEN_RAW_TOOLS, make_find_duplicate_charges

SYSTEM_PROMPT = """你是在线商店的财务运营 Agent，在隔离的任务环境中工作。
使用可用的工具完成任务，直接动手处理，而不是只给建议。"""

MODES = ["baseline", "tool", "mcp", "code", "governance", "full"]
ROOT = Path(__file__).resolve().parents[1]


def own_mcp_server(env: Env):
    """目标 2：以 stdio 子进程启动自己的 MCP server，并把原始 API 的地址交给它。"""
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from strands.tools.mcp import MCPClient

    child_env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG") or k.startswith("AWS_")}
    child_env.update(LAB_API_URL=env.url, LAB_API_HEADERS=json.dumps(env.headers), PYTHONPATH=str(ROOT))
    params = StdioServerParameters(command=sys.executable, args=["-m", "solution.mcp_server"], env=child_env)
    return MCPClient(lambda: stdio_client(params))


def build_tools(env: Env, mode: str) -> list:
    if mode == "mcp":
        return [own_mcp_server(env)]
    if mode == "code":
        # 写操作仍走普通工具；查询交给代码批量完成
        writes = ["refunds_create_v2", "approvals_request", "notifications_send_v2", "notifications_templates_v2"]
        return [env.mcp_client(tool_filters={"allowed": writes}), make_run_python(env)]
    if mode in ("tool", "full"):
        return [env.mcp_client(tool_filters={"rejected": HIDDEN_RAW_TOOLS}), make_find_duplicate_charges(env)]
    return [env.mcp_client()]


def build_hooks(env: Env, mode: str) -> list:
    return [Governance()] if mode in ("governance", "full") else []


def build_agent(env: Env, mode: str) -> Agent:
    return Agent(
        model=build_model(),
        tools=build_tools(env, mode),
        hooks=build_hooks(env, mode),
        system_prompt=SYSTEM_PROMPT,
        callback_handler=None,
    )



if __name__ == "__main__":
    raise SystemExit(run_cli(build_agent, MODES))
