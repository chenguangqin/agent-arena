# 评测 Coding Agent CLI：Kiro CLI 与 Claude Code

在 Harbor 任务上评测 **CLI 本身**（kiro-cli、Claude Code），两者跑在同一套机制下，结果可直接对比。第一个评测方向是代码与二进制安全审核。

## 设计

```text
宿主机                                                   Harbor 任务容器
┌──────────────────────────────────────────┐            ┌──────────────────┐
│ kiro-cli / claude（headless，隔离 HOME） │            │ /app ...         │
│   内置 shell/文件工具全部禁用            │            │ Ghidra, r2, git  │
│        │ MCP (streamable HTTP, 127.0.0.1)│            │                  │
│        ▼                                 │  exec /    │                  │
│ McpBridge: bash / read_file / write_file ├──upload───►│                  │
└──────────────────────────────────────────┘            └──────────────────┘
        凭证（KIRO_API_KEY / AWS）只在宿主机               verifier 检查容器状态
```

- **同一工具面**：每个 trial 起一个 MCP server（随机端口、随机 URL 路径），只有 `bash`、`read_file`、`write_file` 三个工具，全部经 Harbor `environment.exec` / `upload_file` 在任务容器执行。CLI 的内置工具关闭，碰不到宿主机。
- **同一 prompt 与配置**：共享 `BASE_PROMPT`（说明工具在容器里、自主完成），同一个配置目录翻译成各 CLI 的原生格式。
- **隔离**：临时 workspace + 临时 HOME，用户全局的 skills / steering / 会话 / 设置不进入评测；外层 Claude Code / Kiro 会话的环境变量被清除。
- **差异只剩 CLI**：模型相同（Opus 4.6）时，分数差异来自 CLI 的 harness（系统提示、上下文管理、工具调度）。

代码（`arena/arena_agents/`）：

| 文件 | 内容 |
|---|---|
| `host_cli.py` | 公共部分：`McpBridge`、配置目录解析、`HostCliAgent`（进程生命周期、超时/取消时 kill、产物收集） |
| `kiro_host.py` | `KiroHostAgent`：`.kiro/agents/eval.json`、`kiro-cli chat --no-interactive --agent-engine v3` |
| `claude_host.py` | `ClaudeCodeHostAgent`：`claude -p --tools "" --strict-mcp-config --setting-sources project` |
| `terminal_strands.py` | 对照组：Strands + Bedrock 的最简 bash agent |

新增一个 CLI：继承 `HostCliAgent`，实现 `prepare_workspace` / `build_argv` / `build_env` / `summarize` 即可。

## 配置目录

一个配置 = 一个目录，所有内容可选；空目录就是 baseline。

```text
configs/<name>/
├── steering/*.md           # 常驻指导
├── skills/<name>/SKILL.md  # 技能
└── prompt.md               # 追加到 agent/system prompt
```

| 配置项 | kiro-cli | Claude Code |
|---|---|---|
| `steering/*.md`（`inclusion: always` 或无 front matter） | `.kiro/steering/` | 合并为 workspace 的 `CLAUDE.md`（去掉 front matter） |
| `steering/*.md`（`manual` / `fileMatch` / `auto`） | `.kiro/steering/` 原样 | 跳过，记录在 metadata `steering_skipped` |
| `skills/<n>/SKILL.md` | `.kiro/skills/` | `.claude/skills/`（格式相同），并开放 `Skill` 工具 |
| `prompt.md` | agent `prompt` | `--append-system-prompt` |
| 可用工具 | `@container`、`disclose_context` | `mcp__container__*`（有 skill 时加 `Skill`） |
| 认证 | 宿主机 `KIRO_API_KEY` | Bedrock：`CLAUDE_CODE_USE_BEDROCK=1` + 宿主机 AWS 凭证文件；或 `provider=anthropic` + `ANTHROPIC_API_KEY` |

## 运行

前置：Docker、uv（`bash scripts/setup_harbor.sh` 一次装好并做 oracle 冒烟），`cd arena && uv sync`；宿主机装好被测 CLI 及其凭证。

```bash
cd arena
export PYTHONPATH=$PWD          # 让 harbor 导入 arena_agents

# Claude Code（Bedrock）
uv run --frozen harbor run -p <tasks_dir> -i fix-code-vulnerability \
  -a arena_agents.claude_host:ClaudeCodeHostAgent -m global.anthropic.claude-opus-4-6-v1 \
  --ak config_dir=<configs>/baseline -e docker -o <jobs>/claude/baseline --yes

# Kiro CLI
uv run --frozen harbor run -p <tasks_dir> -i fix-code-vulnerability \
  -a arena_agents.kiro_host:KiroHostAgent -m claude-opus-4.6 \
  --ak config_dir=<configs>/baseline -e docker -o <jobs>/kiro/baseline --yes
```

常用 `--ak`：

| 选项 | 适用 | 默认 | 说明 |
|---|---|---|---|
| `config_dir` | 两者 | 空（baseline） | 配置目录 |
| `workdir` | 两者 | `/app` | 容器内 bash 的工作目录 |
| `default_timeout_sec` / `max_timeout_sec` | 两者 | 600 / 1800 | 单次 bash 超时（默认值 / 模型可请求的上限） |
| `run_timeout_sec` | 两者 | 7000 | 超时后 kill CLI；不应超过任务的 agent 超时 |
| `max_output_chars` | 两者 | 20000 | 工具输出截断 |
| `keep_home` | 两者 | false | 保留临时 workspace/HOME 便于排查 |
| `engine` | kiro | `v3` | `--agent-engine` |
| `provider` / `region` | claude | `bedrock` / 宿主机 `AWS_REGION` | 认证方式 |
| `effort` | claude | CLI 默认 | `--effort low…max` |
| `max_turns` / `max_budget_usd` | claude | 不限 | 单 trial 上限 |

`-m` 对 Claude Code 也接受 kiro 写法（`claude-opus-4.6` → `global.anthropic.claude-opus-4-6-v1`），一个 `MODEL` 可同时驱动两个 CLI。

## 产物与指标

每个 trial 的 `agent/` 目录：

| 文件 | 内容 |
|---|---|
| `mcp_calls.jsonl` | 每次桥接工具调用：命令、退出码、输出尾部、耗时（两个 CLI 格式相同，便于对比轨迹） |
| `kiro.jsonl` / `claude.jsonl` | CLI 的 stream-json 原始事件 |
| `*_command.json` | 实际 argv（指令已替换为占位符）与 workspace 文件清单 |
| `kiro_sessions/` / `claude_sessions/` | CLI 自己的会话记录 |
| `final_message.txt` | 最终回复 |

`result.json` 的 agent metadata：`status`、`stop_reason`、`tool_calls`、`tool_failures`、`tools`（按工具计数）、`mcp_calls`、`config_dir`。Claude Code 另有 `cost_usd`、`num_turns`、`mcp_status`，并填写 Harbor 的 `cost_usd` / `n_input_tokens` / `n_cache_tokens` / `n_output_tokens`；kiro 另有 `credits`（v3 引擎不上报，为空）。

CLI 未正常结束（非 success、超时、配额用尽）时 trial 以 `RuntimeError` 记为异常，stderr 尾部写进异常信息——统计时与"做错了"（reward 0）分开看。

## 安全审核任务集

当前实验用 7 个任务：

| 任务 | 来源 | 内容 | 资源 / 超时 |
|---|---|---|---|
| `caddy-timebomb-multiple-binaries-detect` | binary-audit | 4 个 Caddy 二进制中找出植入 timebomb 的 | 4 CPU / 12G / 2h |
| `dnsmasq-backdoor-detect-negative` | binary-audit | dnsmasq 无后门（负样本） | 同上 |
| `dnsmasq-backdoor-detect-obfuscated` | binary-audit | 混淆后门，报函数地址 | 同上 |
| `dropbear-brokenauth-detect` | binary-audit | Dropbear SSH 认证被篡改 | 同上 |
| `lighttpd-backdoor-detect` | binary-audit | lighttpd2 二进制与共享库 | 同上 |
| `fix-code-vulnerability` | terminal-bench 2.0 | Bottle 中找漏洞、给 CWE、修复 | 1 CPU / 2G / 15min |
| `sanitize-git-repo` | terminal-bench 2.0 | 清除仓库中的 API key | 1 CPU / 2G / 15min |

### 已有结果（Opus 4.6）

| 配置 | 通过 | 备注 |
|---|---|---|
| Strands baseline | 8/29（28%） | 单 bash 工具、无 harness 手段 |
| kiro baseline | 12/21（57%） | 4 个异常为 kiro 月度配额用尽（`MONTHLY_REQUEST_COUNT`），非任务失败 |
| kiro + security-steering | 17/21（81%） | 一份 steering：先画攻击面、证据确认、具体 CWE、在校验边界修复、重跑测试 |
| Claude Code baseline | 1/1 | 仅 `fix-code-vulnerability` 冒烟：19 次工具调用、33s、$0.24 |

观察：`fix-code-vulnerability` 的仓库自带一个失败的测试用例，几乎直接指向漏洞，区分度有限；binary-audit 的五个任务更能拉开差距，但每 trial 50–120 次工具调用、时间以十分钟计。

## 添加任务

1. 任务必须是 Harbor 格式（`task.toml`、`instruction.md`、`environment/`、`tests/`），放在任务目录的**第一层**。`harbor download <dataset> -o <dir>` 导出为 `<dir>/<dataset>/<task>/`，需要挪上来一层。
2. 先用 `-a oracle` 跑一遍确认参考答案满分，再用 `harbor check` 看任务质量。
3. 前提：容器内有 `bash` 和 coreutils `timeout`；工作目录不是 `/app` 时传 `--ak workdir=...`；任务能自主完成（τ³ 这类多轮对话 / 专用 MCP 的任务需要专门的 agent，见 `tau3_strands.py`）。
4. 资源：按 `task.toml` 的 `cpus` / `memory` 乘并发数估算；任务的 agent 超时大于 7000s 时调大 `run_timeout_sec`。

## 测试

```bash
cd arena && uv run --frozen pytest tests     # 离线：假容器、脚本化事件流，不花钱
```

覆盖：桥接工具在容器执行、两种 CLI 的 workspace 翻译与 argv、环境变量隔离、stream-json 解析与 metadata。
