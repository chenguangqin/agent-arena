"""Lab2 · Tools 与 MCP —— 这是你要编辑的文件。

运行：
    uv run python agent.py                 # 对话；输入 /task 发送任务，/help 查看命令
    uv run python agent.py --task          # 跑一次任务，打印结果和商店的最终状态
    uv run python agent.py -p "问题"        # 问一个问题后退出

实验目标见 docs/lab.md。你会用到的入口：
    build_tools()   决定模型能用哪些工具（目标 1、2、3）
    build_hooks()   在工具调用前后插入检查（目标 4）
    build_agent()   组装 Agent

env 提供：
    env.mcp_client(**kwargs)  原始 API 的 MCP 客户端，可直接放进 tools 列表
    env.call(name, **args)    在 Python 代码里直接调用一个原始 API，返回解析后的 JSON
    env.url / env.headers     原始 API 的 MCP 地址，供你自己的 MCP server 连接
"""

from __future__ import annotations

from strands import Agent

from labkit import Env, build_model, run_cli

SYSTEM_PROMPT = """你是在线商店的财务运营 Agent，在隔离的任务环境中工作。
使用可用的工具完成任务，直接动手处理，而不是只给建议。"""

# 你可以增加自己的模式，用 --mode 切换，方便对比，例如 "tool"、"mcp"、"code"、"governance"。
MODES = ["baseline"]


def build_tools(env: Env, mode: str) -> list:
    tools: list = [env.mcp_client()]  # 原始 API 的全部接口

    # TODO 目标 1：写一个面向任务的自定义工具（@tool），加入 tools；隐藏不该用的原始接口
    #             （提示：env.mcp_client(tool_filters={"rejected": [...]})）
    # TODO 目标 2：写一个 MCP server（mcp_server.py），用 MCPClient 通过 stdio 连接它
    # TODO 目标 3：写一个在 Docker 沙箱里执行 Python 代码的工具
    return tools


def build_hooks(env: Env, mode: str) -> list:
    # TODO 目标 4：用 hook 拦截废弃接口和未经审批的大额退款，并处理订单备注里的不可信文本
    return []


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
