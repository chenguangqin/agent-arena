"""Lab5 参考答案。

    uv run python -m solution.agent --mode baseline --task    # 单 Agent
    uv run python -m solution.agent --mode workflow --task    # 动态工作流
"""

from __future__ import annotations

from strands import Agent

from labkit import Env, build_model, run_cli
from solution.runner import WorkflowRunner

SYSTEM_PROMPT = """你是在线商店的运维 Agent，在隔离的任务环境中工作。
使用可用的工具检查环境并完成任务，直接动手处理，而不是只给建议。"""

MODES = ["baseline", "workflow"]


def build_agent(env: Env, mode: str):
    if mode == "workflow":
        return WorkflowRunner(env)
    return Agent(model=build_model(), tools=[env.mcp_client()], system_prompt=SYSTEM_PROMPT, callback_handler=None)


if __name__ == "__main__":
    raise SystemExit(run_cli(build_agent, MODES))
