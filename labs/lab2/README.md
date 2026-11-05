# Lab2 · Tools 与 MCP

Strands Agents SDK + Amazon Bedrock 的实验项目。实验目标见 [`docs/lab.md`](docs/lab.md)。

```bash
uv sync
uv run python agent.py --task       # 跑一次任务
uv run python agent.py              # 对话（/task 发送任务，/help 查看命令）
uv run pytest -q                    # 测试
```

| 路径 | 说明 |
|---|---|
| `agent.py` | 你要编辑的文件 |
| `docs/lab.md` / `docs/record.md` | 实验目标 / 对照记录 |
| `labkit/` | 实验基础设施（环境、对话、trace、Arena 入口），不需要修改 |
| `solution/` | 参考答案：`uv run python -m solution.agent --mode full --task` |
| `agent_eval_entry.py`、`agent.toml` | 接入 Arena 评测 |

默认模型 `global.anthropic.claude-haiku-4-5-20251001-v1:0`（`us-west-2`），可用环境变量 `MODEL_ID`、`AWS_REGION` 修改。目标 3 需要本机可用的 Docker。
