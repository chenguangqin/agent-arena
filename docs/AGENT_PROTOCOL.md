# Agent 接入协议

| 项目 | 内容 |
|---|---|
| 协议版本 | Command Adapter v0.2 |
| 来源 | `devopsagent/docs/COMMAND_AGENT_EVALUATION.md`、`devopsagent/evaluation/command_framework/` |
| 适用对象 | 各 lab Agent，以及学员自己开发的任何 Agent（与 SDK 无关） |

本文只摘录 Agent 一侧需要实现的部分。字段定义以 devopsagent 源码为准；两者不一致时，以源码为准并更新本文。

## 1. 评测框架如何看待 Agent

Agent 是一个外部进程。评测框架只负责：

1. 从 Variant 固定的 git commit 创建源码快照，每个 attempt 一份独立副本；
2. 启动 Descriptor 声明的命令，通过 stdin 发送一条请求；
3. 提供 MCP 连接信息，让 Agent 操作任务环境；
4. 在截止时间后终止进程组，读取结果文件。

模型、prompt、循环、MCP 客户端、skills、memory、工作流都由 Agent 自己负责。

## 2. Agent 需要提供的文件

| 文件 | 作用 |
|---|---|
| `agent.toml` | Descriptor：启动命令、状态目录、可调参数 |
| `variants/*.toml` | Variant：固定 commit 和参数取值 |
| 入口脚本 | 读取请求、运行 Agent、写结果文件 |

### 2.1 Descriptor 示例

每个 lab 一个描述文件，完整示例见 `lab5/agent.toml`。要点：

```toml
[source]
repository = ".."            # 相对于描述文件所在目录，即仓库根目录
revision = "git"

[launcher]
command = ["uv", "run", "--frozen", "python", "-m", "lab5", "run"]
result_path = "run/result.json"

[state]
mode = "isolated-copy"
snapshot_paths = []          # Lab4 为 ["lab4/memory", "lab4/skills"]
empty_paths = ["reports", "run"]

[parameters.profile]
type = "enum"
values = ["baseline", "planning", "gate", "gate_sdk", "subagents", "full"]
delivery = "argv"
flag = "--profile"
```

规则：

- Descriptor 中的路径都相对于 `source.repository`，不能是绝对路径，也不能跳出仓库。
- 只有 `[parameters]` 中声明过的参数可以被 Variant 修改。
- `delivery = "argv"` 的参数通过命令行传入，`delivery = "env"` 的参数通过环境变量传入。

### 2.2 Variant 示例

```toml
[variant]
schema_version = "0.2"
id = "lab5-gate"
agent = "../agent.toml"

[source]
commit = "<完整 commit hash>"

[parameters]
profile = "gate"
```

开发时 `commit` 可以写 `HEAD`，但对比记录中保存的是解析后的 commit。工作区有未提交改动时，结果标记为 `reproducible = false`，运行的仍是已提交的快照。

## 3. 运行时输入

### 3.1 stdin 请求

一行 JSON：

```json
{
  "schema_version": "0.2",
  "run_id": "...",
  "variant": {"id": "lab5-gate", "digest": "..."},
  "task": {"id": "case-001", "instruction": "..."},
  "agent_config": {"profile": "gate", "model_id": "..."},
  "paths": {
    "agent_root": "...",
    "artifact_dir": "...",
    "result_path": "..."
  },
  "constraints": {"deadline_seconds": 600}
}
```

### 3.2 环境变量

| 变量 | 含义 |
|---|---|
| `AGENT_EVAL_RUN_ID` | 本次运行 ID |
| `AGENT_EVAL_INPUT` | 请求文件路径 |
| `AGENT_EVAL_RESULT` | 结果文件路径 |
| `AGENT_EVAL_ARTIFACT_DIR` | 产物目录 |
| `AGENT_EVAL_MCP_TRANSPORT` | `streamable-http` |
| `AGENT_EVAL_MCP_URL` | 本次 trial 的 MCP Gateway 地址（仅绑定 loopback） |
| `AGENT_EVAL_MCP_TOKEN` | 本次 trial 的 bearer token |
| `AGENT_EVAL_MCP_HEADERS` | 连接 Gateway 所需的 HTTP header（JSON） |
| 声明为 `env` 的参数 | 如 `MODEL_ID` |

宿主机环境变量只透传进程基础变量、代理设置和标准 AWS 凭证变量。Agent 如果加载 `.env`，必须使用不覆盖已有变量的方式（如 `load_dotenv(..., override=False)`）。

## 4. 运行时输出

Agent 必须写入 `AGENT_EVAL_RESULT` 指向的文件：

```json
{
  "schema_version": "0.2",
  "status": "completed",
  "final_text": "...",
  "structured_output": null,
  "usage": {
    "input_tokens": 12345,
    "output_tokens": 678,
    "source": "sdk"
  },
  "artifacts": ["reports/trace.jsonl"]
}
```

- `status` 只能是 `completed` 或 `error`。
- 拿不到的指标写 `null`，不能写 0。
- Agent 自报的 usage 只用于诊断，不作为排名依据。
- 超过截止时间的运行一律记 0 分；已写出的部分产物仍可供诊断。

## 5. 状态隔离

- `snapshot_paths` 中的目录（Lab4 为 `lab4/memory`、`lab4/skills`）在每个 attempt 开始时从提交的快照复制一份；运行中的修改不会回写源仓库，也不会流入其他 attempt。
- `empty_paths` 中的目录在每个 attempt 开始时为空。

## 6. Lab Agent 在协议之外的约定

以下不是协议要求，是 lab Agent 为课程讲解和复盘额外输出的内容，学员 Agent 可以选择是否实现：

| 产物 | 内容 |
|---|---|
| `reports/trace.jsonl` | 每次模型调用和工具调用的事件记录（格式见 `LABS_SPEC.md` §3.4） |
| `reports/summary.json` | 生效的功能、token 与上下文峰值、各类功能事件计数、结束原因 |
