# 给 code agent 的说明

这是一个教学实验项目（Strands Agents SDK + Amazon Bedrock）。学员通过修改 `agent.py` 和 `workflow.py` 完成 `docs/lab.md` 中的目标：实现一个动态工作流。

## 可以修改
- `agent.py`、`workflow.py`，以及新建的模块
- `docs/record.md`（对照记录）

## 不要修改
- `labkit/`：实验基础设施（环境、chat CLI、trace、Arena 入口）
- `agent_eval_entry.py`、`agent.toml`
- `solution/`：参考答案，学员自己完成前不要参考

## 约定
- 工作流定义必须是声明式数据（JSON / pydantic 模型），不要执行模型生成的代码。
- 自己创建的 Agent 用 `build_model(label="角色名")` 和 `hooks=[trace.Tracer("角色名")]`，token 才会按角色统计（`/stats` 中的 `by_agent`）。
- 节点 Agent 用 `env.mcp_client(tool_filters={"allowed": [...]})` 只拿到本步骤需要的工具。
- 用 `trace.event("名字", **字段)` 记录工作流事件（生成的工作流、步骤开始/结束、重新规划、完成检查）。
- 运行：`uv run python agent.py --mode workflow --task`；测试：`uv run pytest -q`。
