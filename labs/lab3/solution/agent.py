"""Lab3 参考答案。

    uv run python -m solution.agent --mode full --task
    uv run python -m solution.agent --mode baseline        # 对话；试试 /context 和 /compact

模式：
    baseline   环境的全部工具（整段读取日志和 runbook）；/context、/compact 在所有模式下都可用
    compact    目标 2：上下文超过阈值时自动压缩
    offload    目标 3：大结果卸载到文件
    retrieval  目标 4：按需检索，隐藏整段读取的工具
    full       目标 2 + 3 + 4
"""

from __future__ import annotations

from strands import Agent

from labkit import Env, build_model, run_cli
from solution.commands import handle_command
from solution.compaction import Compactor
from solution.offload import Offload, make_offload_readers
from solution.retrieval import BULK_TOOLS, make_retrieval_tools

SYSTEM_PROMPT = """你是在线商店的值班 SRE Agent，在隔离的任务环境中排查故障。
使用可用的工具检查日志、配置和 runbook，直接动手处理，而不是只给建议。"""

RETRIEVAL_GUIDE = """
查日志用 query_logs（按服务、级别、时间窗、关键词过滤，返回计数和样例）；查 runbook 用 search_runbooks，注意文档的更新日期。"""

MODES = ["baseline", "compact", "offload", "retrieval", "full"]
AUTO_COMPACT_TOKENS = 60_000


def build_tools(env: Env, mode: str) -> list:
    if mode in ("retrieval", "full"):
        tools: list = [env.mcp_client(tool_filters={"rejected": BULK_TOOLS}), *make_retrieval_tools(env)]
    else:
        tools = [env.mcp_client()]
    if mode in ("offload", "full"):
        tools += make_offload_readers()
    return tools


def build_hooks(env: Env, mode: str) -> list:
    return [Offload()] if mode in ("offload", "full") else []


def build_agent(env: Env, mode: str) -> Agent:
    kwargs = {}
    if mode in ("compact", "full"):
        kwargs["conversation_manager"] = Compactor(threshold_tokens=AUTO_COMPACT_TOKENS)
    prompt = SYSTEM_PROMPT + (RETRIEVAL_GUIDE if mode in ("retrieval", "full") else "")
    return Agent(
        model=build_model(),
        tools=build_tools(env, mode),
        hooks=build_hooks(env, mode),
        system_prompt=prompt,
        callback_handler=None,
        **kwargs,
    )


if __name__ == "__main__":
    raise SystemExit(run_cli(build_agent, MODES, handle_command))
