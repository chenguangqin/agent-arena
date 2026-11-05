# Agent Arena · 课程实验

《AI Agent 开发实战：Harness Engineering 与评测》的实验项目。基于 Strands Agents SDK 与 Amazon Bedrock。

## 实验

每个 lab 是一个独立项目，进入目录后 `uv sync` 即可运行。学员编辑 `agent.py`，实验目标见各 lab 的 `docs/lab.md`，参考答案在 `solution/`。

| Lab | 主题 | 场景 |
|---|---|---|
| [`labs/lab2`](labs/lab2) | Tools 与 MCP：自定义工具、MCP server、沙箱代码执行、治理 hook | 重复扣款对账 |
| [`labs/lab3`](labs/lab3) | Context engineering：slash command、`/context`、`/compact`、卸载、检索 | checkout 间歇失败排查 |
| [`labs/lab4`](labs/lab4) | Skills 与长期记忆：采集、抽取、治理、使用 | 连续处理 10 张客服工单 |
| [`labs/lab5`](labs/lab5) | 动态工作流：编排 Agent 生成工作流，确定性引擎执行 | 大促事故处理 |

```bash
cd labs/lab2
uv sync
uv run python agent.py --task                        # 跑一次 baseline
uv run python -m solution.agent --mode full --task   # 运行参考答案
```

需要可调用 Bedrock 的 AWS 凭证（默认 `us-west-2`）；Lab2 的代码执行目标需要 Docker。

## 文档

| 文件 | 内容 |
|---|---|
| [`docs/LAB_GOALS.md`](docs/LAB_GOALS.md) | 各 lab 的实验目标 |
| [`docs/LABS_SPEC.md`](docs/LABS_SPEC.md) | lab 结构、扩展点、实现约定、验证状态 |
| [`docs/AGENT_PROTOCOL.md`](docs/AGENT_PROTOCOL.md) | 接入 Arena 评测的协议（Command Adapter v0.2） |
| [`docs/OBSERVATIONS.md`](docs/OBSERVATIONS.md) | 真实运行中观察到的现象，可用作课件素材 |
| [`docs/STRANDS_NOTES.md`](docs/STRANDS_NOTES.md) | Strands 1.60 能力核查 |
| [`docs/EVAL_GUIDE.md`](docs/EVAL_GUIDE.md) | **评测操作手册**：Harbor 环境、τ³ banking 任务、Strands baseline、结果判读 |
| [`docs/ARENA_SPEC.md`](docs/ARENA_SPEC.md) | Arena 基础设施设计（草案，暂缓） |

早期基于 profile 开关的实现保存在 git tag `v0-profile-labs`。
