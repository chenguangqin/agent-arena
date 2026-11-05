# 给 code agent 的说明

这是一个教学实验项目（Strands Agents SDK + Amazon Bedrock）。学员通过修改 `agent.py` 完成 `docs/lab.md` 中的目标。

## 可以修改
- `agent.py`：`build_agent()`、`on_session_end()`、`COMMANDS`、`MODES`、`SYSTEM_PROMPT`
- `skills/` 下的 skill（`<名字>/SKILL.md`，可带 `references/`、`scripts/`、`assets/`）
- 新建的模块，例如 `memory.py`
- `docs/record.md`（对照记录）

## 不要修改
- `labkit/`：实验基础设施（客服环境、工单队列与会话循环、chat CLI、trace、Arena 入口）
- `agent_eval_entry.py`、`agent.toml`
- `solution/`：参考答案，学员自己完成前不要参考

## 约定
- 每张工单是一个新会话：labkit 为每张工单调用一次 `build_agent(env, mode, session)`，结束后调用 `on_session_end(env, mode, session)`。
- 跨会话保留的只有 `session.memory_dir`（默认 `./memory`）。记忆以 Markdown 保存。
- 会话结束后 `session.trajectory` 提供 `messages`、`tool_calls`（name、input、status、result）、`reply`、`submitted`、`feedback`。
- 调用客服系统：`env.mcp_client()` 放进 Agent 的 tools；在 Python 中直接调用用 `env.call(name, **args)`。
- 自定义事件用 `from labkit import trace; trace.event("名字", **字段)` 记录。
- 自定义 chat 命令：在 `COMMANDS` 中加入 `"/名字": 函数(arg: str, ctx: CommandContext)`。
- 运行：`uv run python agent.py --task --reset-memory`；测试：`uv run pytest -q`。
