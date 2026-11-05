# 给 code agent 的说明

这是一个教学实验项目（Strands Agents SDK + Amazon Bedrock）。学员通过修改 `agent.py` 完成 `docs/lab.md` 中的目标。

## 可以修改
- `agent.py`：`handle_command()`、`build_tools()`、`build_hooks()`、`build_agent()`、`MODES`、`SYSTEM_PROMPT`
- 新建的模块，例如 `commands.py`、`compaction.py`、`offload.py`、`retrieval.py`
- `docs/record.md`（对照记录）

## 不要修改
- `labkit/`：实验基础设施（环境、chat CLI、trace、Arena 入口）
- `agent_eval_entry.py`、`agent.toml`
- `solution/`：参考答案，学员自己完成前不要参考

## 约定
- 运行：`uv run python agent.py`（对话）、`uv run python agent.py --task`（跑一次任务并打印环境最终状态）
- chat 内置命令只有 `/task`、`/state`、`/help`、`/reset`、`/quit`；其他 `/...` 输入由 `agent.py` 的 `handle_command(agent, line)` 处理，处理了返回 True
- 环境工具通过 `env.mcp_client()` 提供给 Agent；在 Python 代码中调用环境工具用 `env.call(name, **args)`
- 上一次模型调用的实际 token：`labkit.trace.last_usage()`；自定义事件：`labkit.trace.event("名字", **字段)`
- 压缩时不能拆开 toolUse 与对应的 toolResult，消息角色要保持 user / assistant 交替
- 测试：`uv run pytest -q`
