"""Lab3 · Context engineering —— 这是你要编辑的文件。

运行：
    uv run python agent.py                 # 对话；/task 发送任务，/help 查看命令
    uv run python agent.py --task          # 跑一次任务，打印 token 统计和环境的最终状态
    uv run python agent.py -p "问题"        # 问一个问题后退出

实验目标见 docs/lab.md。你会用到的入口：
    handle_command()  chat 中以 "/" 开头、且不是内置命令的输入都会交给它（目标 1、2：/context、/compact）
    build_tools()     决定模型能用哪些工具（目标 4：检索工具）
    build_hooks()     在工具调用前后插入处理（目标 3：大结果卸载）
    build_agent()     组装 Agent（目标 2：自动压缩可以放在 conversation_manager 里）

env 提供：
    env.mcp_client(**kwargs)  环境工具的 MCP 客户端，可直接放进 tools 列表
    env.call(name, **args)    在 Python 代码里直接调用一个环境工具
"""

from __future__ import annotations

from strands import Agent

from labkit import Env, build_model, run_cli

SYSTEM_PROMPT = """你是在线商店的值班 SRE Agent，在隔离的任务环境中排查故障。
使用可用的工具检查日志、配置和 runbook，直接动手处理，而不是只给建议。"""

# 你可以增加自己的模式，用 --mode 切换，方便对比，例如 "offload"、"retrieval"、"compact"。
MODES = ["baseline"]


def handle_command(agent: Agent, line: str) -> bool:
    """处理一条 slash command。处理了返回 True，不认识返回 False。

    line 是用户输入的整行，例如 "/compact 保留排查结论"。收到 "/help" 时，列出你自己的命令。
    """
    # TODO 目标 1：设计一个命令注册机制（命令名、说明、处理函数），实现 /context：
    #             显示当前上下文的构成（system prompt、工具定义、对话消息、工具结果各占多少 token）
    # TODO 目标 2：实现 /compact [保留重点]：把对话压缩成结构化摘要，替换历史消息
    return False


def build_tools(env: Env, mode: str) -> list:
    tools: list = [env.mcp_client()]  # 环境提供的全部工具（日志、runbook、配置、重放）
    # TODO 目标 4：写按需检索的工具（日志按条件过滤、runbook 检索），并隐藏整段读取的工具
    return tools


def build_hooks(env: Env, mode: str) -> list:
    # TODO 目标 3：工具结果太大时，完整内容写入文件，上下文里只留预览和文件路径
    return []


def build_agent(env: Env, mode: str) -> Agent:
    return Agent(
        model=build_model(),
        tools=build_tools(env, mode),
        hooks=build_hooks(env, mode),
        system_prompt=SYSTEM_PROMPT,
        callback_handler=None,
        # TODO 目标 2：上下文超过阈值时自动压缩（提示：自定义 ConversationManager）
    )


if __name__ == "__main__":
    raise SystemExit(run_cli(build_agent, MODES, handle_command))
