# 给 code agent 的说明

这是一个教学实验项目（Strands Agents SDK + Amazon Bedrock）。学员通过修改 `agent.py` 完成 `docs/lab.md` 中的目标。

## 可以修改
- `agent.py`：`build_tools()`、`build_hooks()`、`build_agent()`、`MODES`、`SYSTEM_PROMPT`
- 新建的模块，例如 `mcp_server.py`、`code_exec.py`、`governance.py`
- `docs/record.md`（对照记录）

## 不要修改
- `labkit/`：实验基础设施（环境、chat CLI、trace、Arena 入口）
- `agent_eval_entry.py`、`agent.toml`
- `solution/`：参考答案，学员自己完成前不要参考

## 约定
- 运行：`uv run python agent.py`（对话）、`uv run python agent.py --task`（跑一次任务并打印商店最终状态）
- 原始 API 通过 `env.mcp_client()` 提供给 Agent；在 Python 代码中调用原始 API 用 `env.call(name, **args)`
- 自定义事件用 `from labkit import trace; trace.event("名字", **字段)` 记录，会显示在 chat 中并写入 `reports/trace.jsonl`
- 代码执行必须在 Docker 沙箱中进行；Docker 不可用时必须拒绝执行，不能在本机直接运行模型生成的代码
- 测试：`uv run pytest -q`
