"""Lab5 · 动态工作流 —— 这是你要编辑的文件（以及 workflow.py）。

运行：
    uv run python agent.py --task                   # 单 Agent baseline 跑一次任务
    uv run python agent.py --mode workflow --task   # 你的动态工作流
    uv run python agent.py --mode workflow          # 对话；/task 发送任务，/stats 看每个 Agent 的 token

实验目标见 docs/lab.md。build_agent() 可以返回两种东西：
    * 一个 Strands Agent（baseline）
    * 任何实现了 __call__(message) -> str 的对象（你的工作流运行器），可选 cleanup()、tool_names

env 提供：
    env.mcp_client(**kwargs)   环境工具的 MCP 客户端；tool_filters={"allowed": [...]} 只给节点需要的工具
    env.tool_catalog()         所有环境工具的名称和描述（给编排 Agent 看）
    env.call(name, **args)     在 Python 代码里直接调用一个环境工具（例如做确定性检查）
"""

from __future__ import annotations

from strands import Agent

from labkit import Env, build_model, run_cli

SYSTEM_PROMPT = """你是在线商店的运维 Agent，在隔离的任务环境中工作。
使用可用的工具检查环境并完成任务，直接动手处理，而不是只给建议。"""

MODES = ["baseline", "workflow"]


def build_agent(env: Env, mode: str):
    if mode == "workflow":
        from workflow import WorkflowRunner

        return WorkflowRunner(env)
    return Agent(model=build_model(), tools=[env.mcp_client()], system_prompt=SYSTEM_PROMPT, callback_handler=None)


if __name__ == "__main__":
    raise SystemExit(run_cli(build_agent, MODES))
