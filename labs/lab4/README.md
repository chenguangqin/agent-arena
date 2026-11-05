# Lab4 · Skills 与长期记忆

Strands Agents SDK + Amazon Bedrock 的实验项目。实验目标见 [`docs/lab.md`](docs/lab.md)。

```bash
uv sync
uv run python agent.py --task --reset-memory   # 从空记忆开始处理 10 张工单
uv run python agent.py                         # 对话（/next 处理下一张工单，/help 查看命令）
uv run pytest -q                               # 测试
```

| 路径 | 说明 |
|---|---|
| `agent.py` | 你要编辑的文件：`build_agent()`、`on_session_end()`、`COMMANDS` |
| `skills/` | 你写的 skill |
| `memory/` | 长期记忆（跨会话保留） |
| `docs/lab.md` / `docs/record.md` | 实验目标 / 对照记录 |
| `labkit/` | 实验基础设施，不需要修改 |
| `solution/` | 参考答案：`uv run python -m solution.agent --mode full --task --reset-memory` |
| `agent_eval_entry.py`、`agent.toml` | 接入 Arena 评测 |

默认模型 `global.anthropic.claude-haiku-4-5-20251001-v1:0`（`us-west-2`），可用环境变量 `MODEL_ID`、`AWS_REGION` 修改。
