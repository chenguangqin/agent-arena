"""Lab4 · Skills 与长期记忆 —— 这是你要编辑的文件。

运行：
    uv run python agent.py --task --reset-memory   # 从空记忆开始处理全部 10 张工单，打印每张工单的结果
    uv run python agent.py                         # 对话：/next 在新会话中处理下一张工单，/help 查看命令

每张工单都在一个新会话中处理（新的 Agent、空的上下文）。工单之间唯一能保留的，
是你写进 session.memory_dir 的内容。

你会用到的入口（实验目标见 docs/lab.md）：
    build_agent(env, mode, session)      每个会话开始时调用，组装 Agent（目标 1：skill；目标 2：使用记忆）
    on_session_end(env, mode, session)   每个会话结束后调用（目标 2：采集 → 抽取 → 治理）
    COMMANDS                             你自己的 chat 命令，例如 /skills、/memory、/forget

session 提供：
    session.ticket          本次工单（id、customer_id、order_id、subject、body）；自由对话时为 None
    session.prompt          发给 Agent 的工单任务文本
    session.memory_dir      记忆目录（默认 ./memory），跨会话保留
    session.skills_dir      skill 目录（默认 ./skills）
    session.trajectory      会话结束后可用：messages、tool_calls、reply、submitted、feedback（质检意见）
"""

from __future__ import annotations

from strands import Agent

from labkit import CommandContext, Env, Session, build_model, run_cli

SYSTEM_PROMPT = """你是在线商店的客服 Agent，在隔离的任务环境中工作。
按公司政策处理工单：需要时用工具查询，直接动手处理，完成后提交处理结果。"""

# 你可以增加自己的模式，用 --mode 切换，方便对比，例如 "skills"、"memory"、"full"。
MODES = ["baseline"]


def build_agent(env: Env, mode: str, session: Session) -> Agent:
    tools: list = [env.mcp_client()]  # 客服系统的全部工具（含 read_policy：读取政策全文）
    plugins: list = []
    system_prompt = SYSTEM_PROMPT

    # TODO 目标 1：把三份政策写成 skill（skills/<名字>/SKILL.md），用 Strands 的 AgentSkills 接入
    #             （from strands.vended_plugins.skills import AgentSkills）
    # TODO 目标 2（使用）：从 session.memory_dir 检索与本工单相关的记忆，放进 system prompt 或任务文本

    return Agent(model=build_model(), tools=tools, plugins=plugins, system_prompt=system_prompt,
                 callback_handler=None)


def on_session_end(env: Env, mode: str, session: Session) -> None:
    # TODO 目标 2（采集 → 抽取 → 治理）：
    #   采集：保存 session.trajectory
    #   抽取：用模型从轨迹中抽取值得长期记住的内容（事实、偏好、经验教训），写入 session.memory_dir 下的 Markdown
    #   治理：去重、消歧（新旧冲突时怎么办）、遗忘
    pass


def show_skills(arg: str, ctx: CommandContext) -> None:
    print("TODO：列出 ctx.skills_dir 下的 skill，以及上一个会话加载了哪些 skill")


COMMANDS = {"/skills": show_skills}  # TODO 目标 2：增加 /memory、/forget <关键词>


if __name__ == "__main__":
    raise SystemExit(run_cli(build_agent, on_session_end, MODES, COMMANDS))
