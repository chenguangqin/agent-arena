# Lab5 · 动态工作流

Strands Agents SDK + Amazon Bedrock 的实验项目。实验目标见 [`docs/lab.md`](docs/lab.md)。

```bash
uv sync
uv run python agent.py --task                   # 单 Agent baseline
uv run python agent.py --mode workflow --task   # 你的动态工作流
uv run python agent.py --mode workflow          # 对话（/task 发送任务，/help 查看命令）
uv run pytest -q
```

| 路径 | 说明 |
|---|---|
| `agent.py`、`workflow.py` | 你要编辑的文件；`workflow.py` 是建议的骨架 |
| `docs/lab.md` / `docs/record.md` | 实验目标 / 对照记录 |
| `labkit/` | 实验基础设施，不需要修改 |
| `solution/` | 参考答案：`uv run python -m solution.agent --mode workflow --task` |
| `agent_eval_entry.py`、`agent.toml` | 接入 Arena 评测 |

默认模型 `global.anthropic.claude-haiku-4-5-20251001-v1:0`（`us-west-2`），可用环境变量 `MODEL_ID`、`AWS_REGION` 修改。
