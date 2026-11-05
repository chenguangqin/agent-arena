"""Lab4 参考答案。

    uv run python -m solution.agent --mode full --task --reset-memory
    uv run python -m solution.agent --mode full          # 对话：/next、/skills、/memory、/forget

模式：
    baseline        客服工具 + read_policy（政策全文按需读取）
    sop_in_prompt   对照组：三份政策全文放进 system prompt
    skills          目标 1：AgentSkills 按需加载（元数据 → 正文 → 资源文件）
    memory          目标 2：长期记忆 pipeline
    full            目标 1 + 目标 2
"""

from __future__ import annotations

from pathlib import Path

from strands import Agent

from labkit import Env, Session, build_model, run_cli
from solution import memory
from solution.skills_support import make_read_skill_file, show_skills, skills_plugin

SYSTEM_PROMPT = """你是在线商店的客服 Agent，在隔离的任务环境中工作。
按公司政策处理工单：需要时用工具查询，直接动手处理，完成后提交处理结果。"""

MODES = ["baseline", "sop_in_prompt", "skills", "memory", "full"]
SKILLS_DIR = Path(__file__).resolve().parent / "skills"
_POLICY_TEXT: dict[str, str] = {}


def all_policies(env: Env) -> str:
    """Control group: every policy document in full (read once per environment)."""
    key = env.url
    if key not in _POLICY_TEXT:
        names = [p["name"] for p in env.call("list_policies")]
        _POLICY_TEXT[key] = "\n\n".join(env.call("read_policy", policy=n) for n in names)
    return _POLICY_TEXT[key]


def build_agent(env: Env, mode: str, session: Session) -> Agent:
    tools: list = [env.mcp_client()]
    plugins: list = []
    system_prompt = SYSTEM_PROMPT
    if mode == "sop_in_prompt":
        system_prompt += "\n\n## 公司政策（全文）\n" + all_policies(env)
    if mode in ("skills", "full"):
        # read_policy 隐藏起来，政策只通过 skill 获取
        tools = [env.mcp_client(tool_filters={"rejected": ["read_policy", "list_policies"]}),
                 make_read_skill_file(SKILLS_DIR)]
        plugins.append(skills_plugin(SKILLS_DIR))
    if mode in ("memory", "full"):
        system_prompt += memory.memory_prompt(session)  # push: relevant memories at session start
        system_prompt += "\n\n遇到不熟悉的系统现象、或客户描述与系统记录不一致时，先用 recall 查询长期记忆。"
        tools.append(memory.make_recall(session))  # pull: search memory mid-session
    return Agent(model=build_model(), tools=tools, plugins=plugins, system_prompt=system_prompt,
                 callback_handler=None)


def on_session_end(env: Env, mode: str, session: Session) -> None:
    if mode in ("memory", "full"):
        memory.end_session(session)


COMMANDS = {"/skills": show_skills, "/memory": memory.show_memory, "/forget": memory.forget_command}

if __name__ == "__main__":
    raise SystemExit(run_cli(build_agent, on_session_end, MODES, COMMANDS, skills_dir=SKILLS_DIR))
