# Arena 评测：Harbor + Strands baseline

外部 benchmark 的接入与 baseline Agent。第一个接入的是 τ³-bench 的 `banking_knowledge`（金融科技客服：约 700 篇知识文档、BM25 检索、修改账户，与模拟用户多轮对话）。

## 运行

```bash
bash scripts/setup_harbor.sh        # Docker / Harbor / oracle 冒烟（一次）
bash scripts/prepare_tau3.sh        # 生成 τ³ banking_knowledge 任务（一次）
bash scripts/run_tau3_baseline.sh   # 用 Strands baseline 跑第 1 个任务
```

常用变量：`N_TASKS=5`、`ATTEMPTS=3`、`TASKS="<任务目录名> ..."`、`MODEL=...`（默认 `global.anthropic.claude-opus-4-6-v1`）、`USER_SIM=bedrock|openai`。

## 结构

```text
arena/
├── arena_agents/
│   ├── tau3_strands.py     # Harbor Agent：Strands + Bedrock，跑在宿主机
│   ├── mcp_call.py         # 在任务容器内执行：调用一次 MCP 工具，输出 JSON
│   ├── patch_tau3_tasks.py # 给生成的任务加 Bedrock 变量（模拟用户、判定器）
│   └── bedrock_token.py    # 生成短期 Bedrock API key
└── tests/                  # 离线测试：假环境 + 脚本化模型
```

## 设计要点

- **工具通道**：τ³ 的 `tau3-runtime` MCP 服务只在任务的私有 Docker 网络里（`expose`，不发布端口）。Agent 在宿主机运行，每次工具调用经 `environment.exec` 在主容器中执行 `mcp_call.py`。AWS 凭证不进容器。
- **对话循环由 harness 驱动**：`start_conversation` 拿到用户第一句话 → Agent 调工具、给出回复 → `submit_assistant_message` 发给模拟用户，拿到下一句 → 直到 runtime 返回结束原因、Agent 调用 `end_conversation` 或达到 `max_user_turns`。
- **只给模型看领域工具**和 `end_conversation`；`configure_run`、`submit_*`、`record_termination` 等 harness 工具不暴露。
- **Baseline 刻意从简**：没有 prompt caching、上下文管理、重试、额外提示，system prompt 只有任务 policy。课程中的 harness 手段在这个基础上逐项叠加。
- **模拟用户与判定器**：在任务容器内通过 LiteLLM 调模型。默认走 Bedrock（Haiku 4.5），宿主机生成短期 Bedrock API key 传进 runtime 和 verifier；也可以 `USER_SIM=openai` 用上游默认的 `gpt-5.2`，与公开结果可比。

## 产物

每个 trial 的 `agent/` 目录：`conversation.jsonl`（对话）、`mcp_calls.jsonl`（每次工具调用、参数、结果、耗时）、`strands_messages.json`（完整消息）。`result.json` 的 agent metadata 中有工具调用数、错误数、对话轮数、模型调用次数和结束原因。
