# Arena 评测操作手册

用 Harbor 在 Docker 中运行外部 benchmark，评测 Strands Agent。当前接入的是 τ³-bench 的 `banking_knowledge` 域：金融科技客服，Agent 与模拟用户多轮对话，检索约 700 篇知识文档，按需修改账户，最后按数据库状态和动作打分。

| 项目 | 内容 |
|---|---|
| 评测框架 | Harbor 0.24.0 |
| 任务来源 | Harbor `adapters/tau3-bench`（v0.24.0）+ tau2-bench **v1.0.1** |
| 被测 Agent | `arena/arena_agents/tau3_strands.py`（Strands 1.60，Bedrock） |
| 被测模型 | `global.anthropic.claude-opus-4-6-v1`（`us-west-2`） |
| 模拟用户 / 判定 | `bedrock/converse/global.anthropic.claude-haiku-4-5-20251001-v1:0`（LiteLLM） |

## 1. 前置条件

- Linux（已验证 Amazon Linux 2023，x86_64）或 macOS（用 Colima 代替 Docker Desktop）。至少 4 核、8 GB 内存、30 GB 可用磁盘。
- 能访问 GitHub、ghcr.io、Docker Hub、PyPI、Bedrock `us-west-2`。
- 宿主机有 AWS 凭证（实例角色即可），能调用 Bedrock，并能生成 Bedrock API key（短期 token）。
- Linux 上需要 sudo（安装 Docker 及其插件、加入 docker 组）。

## 2. 三步运行

```bash
git clone https://github.com/chenguangqin/agent-arena.git && cd agent-arena

bash scripts/setup_harbor.sh        # ① 环境：Docker、Compose/Buildx、uv、Harbor，跑两个 oracle 冒烟任务
bash scripts/prepare_tau3.sh        # ② 生成 τ³ banking_knowledge 任务（97 个）并打补丁
bash scripts/run_tau3_baseline.sh   # ③ 用 Strands baseline 跑第 1 个任务
```

三个脚本都可以重复执行，已完成的步骤会跳过。

| 步骤 | 首次耗时（EC2 参考） | 成功标志 |
|---|---|---|
| ① setup_harbor | 约 5 分钟 | `hello-world` 和 `log-summary-date-ranges` 的 oracle 得分都是 `1.000` |
| ② prepare_tau3 | 约 1 分钟 | `完成：97 个任务在 ~/.agent-arena/harbor/datasets/tau3-bench` |
| ③ run_tau3_baseline | 首题 5–10 分钟（含镜像构建），之后每题约 2–10 分钟 | 结果表中 `Exceptions` 为 0 |

新开终端单独使用 `harbor` 命令前，先执行 `source ~/.agent-arena/harbor/env.sh`。

## 3. 运行参数

`run_tau3_baseline.sh` 的环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `N_TASKS` | 1 | 按目录名排序取前 N 个任务 |
| `TASKS` | — | 指定任务目录名，空格分隔，例如 `"tau3-banking_knowledge-task-001 tau3-banking_knowledge-task-007"` |
| `ATTEMPTS` | 1 | 每个任务运行次数（用于 pass^k） |
| `AGENT` | `arena_agents.tau3_strands:StrandsTau3Agent` | Harbor Agent 导入路径（各队的 Agent 见第 6 节） |
| `MODEL` | `global.anthropic.claude-opus-4-6-v1` | 被测 Agent 的 Bedrock 模型 |
| `N_CONCURRENT` | 1 | 同时运行的 trial 数（所有任务在一次 harbor run 中） |
| `JOBS` | `jobs/tau3-baseline`，其他 Agent 为 `jobs/tau3-<类名>` | 结果目录 |
| `USER_SIM` | `bedrock` | `bedrock` 或 `openai`（`openai` 需要 `OPENAI_API_KEY`，默认 `gpt-5.2`，与上游公开结果可比） |
| `USER_MODEL` | 见上表 | 模拟用户和判定器的 LiteLLM 模型名 |
| `AWS_REGION` | `us-west-2` | |

`prepare_tau3.sh` 的环境变量：`TAU3_DOMAINS`（默认 `banking_knowledge`，可加 `airline,retail,telecom`）、`TAU3_LIMIT`（只生成前 N 个）、`TAU2_REF`（默认 `v1.0.1`）、`HARBOR_REF`（默认 `v0.24.0`）。

`setup_harbor.sh` 的环境变量：`HARBOR_VERSION`、`HARBOR_HOME`（默认 `~/.agent-arena/harbor`）、`COLIMA_CPU/MEMORY/DISK`、`SKIP_TB_SAMPLE=1`。

示例：

```bash
N_TASKS=10 bash scripts/run_tau3_baseline.sh             # 10 题各 1 次，看整体通过率与成本
TASKS="tau3-banking_knowledge-task-003" ATTEMPTS=3 bash scripts/run_tau3_baseline.sh
AGENT=arena_agents.team_a:TeamAgent N_TASKS=10 ATTEMPTS=3 N_CONCURRENT=4 bash scripts/run_tau3_baseline.sh
```

并发数受机器资源限制：每个 trial 起两个容器（任务声明 4 核、8 GB），并且同时调用 Bedrock。8 核 30 GB 的机器建议不超过 4。

Agent 自身的参数通过 `--ak key=value` 传入（在脚本里修改），可用 `harbor agent schema arena_agents.tau3_strands:StrandsTau3Agent` 查看：`region`、`max_tokens`（4096）、`max_user_turns`（30）、`tool_timeout_sec`（240）。

## 4. 查看结果

结果目录：`~/.agent-arena/harbor/jobs/tau3-baseline/<时间>/`，每个 trial 一个子目录。

| 文件 | 内容 |
|---|---|
| `result.json`（job 级） | 汇总：trial 数、异常数、平均 reward |
| `<trial>/result.json` | 单个 trial：reward、token、agent metadata（工具调用数、错误数、对话轮数、模型调用次数、结束原因） |
| `<trial>/verifier/result.json` | 判定细节：`status`、`reward_basis`、各项检查结果 |
| `<trial>/agent/conversation.jsonl` | Agent 与模拟用户的对话 |
| `<trial>/agent/mcp_calls.jsonl` | 每次工具调用的参数、结果、是否出错、耗时 |
| `<trial>/agent/strands_messages.json` | Strands 的完整消息历史 |
| `<trial>/exception.txt` | 出现异常时的堆栈 |

常用命令：

```bash
J=$(ls -d ~/.agent-arena/harbor/jobs/tau3-baseline/*/ | tail -1)
T=$(find $J -mindepth 1 -maxdepth 1 -type d | head -1)

cat $T/verifier/result.json                    # 为什么得这个分
cat $T/agent/conversation.jsonl                # 对话
python3 -c "import json;[print(r['tool'], json.dumps(r['arguments'],ensure_ascii=False)[:150], '| ERR' if r['is_error'] else '') for r in map(json.loads, open('$T/agent/mcp_calls.jsonl'))]"
find $J -name exception.txt -exec tail -30 {} \;

uv run --project arena harbor view ~/.agent-arena/harbor/jobs/tau3-baseline   # 网页查看（本机 Viewer）
```

判读：

- **`Exceptions` 不为 0**：基础设施或 Agent 代码出错，reward 不可信，先看 `exception.txt`。
- **verifier `status` 不是正常值**（例如 `tau2_runtime_log_error`）：判定器自身出错，0 分不代表 Agent 做错。
- **reward 为 0 且判定正常**：对照 `reward_basis`（如 `DB`、`ACTION`）看是数据库最终状态不对，还是缺少/多做了某个动作，再到对话和工具调用里找原因。
- 单题单次随机性很大。比较 harness 改动时，至少用同一组任务、每题 3 次，看平均 reward 和 pass^k。

## 5. 架构

```text
宿主机
├── Harbor（arena/.venv）
└── StrandsTau3Agent ── Bedrock（Opus 4.6，凭证只在宿主机）
      │ environment.exec("python3 /opt/arena/mcp_call.py <url> <tool> <args>")
      ▼
任务的 Docker Compose 网络（不发布端口）
├── main 容器：mcp_call.py → MCP 请求；verifier 在这里运行
└── tau3-runtime 容器：MCP 服务（领域工具 + 对话状态），模拟用户经 LiteLLM 调 Bedrock（Haiku 4.5）
```

- τ³ 的 MCP 服务只在任务的私有网络中（`expose`，不发布端口），宿主机无法直连，所以每次工具调用经 `environment.exec` 在 main 容器中转发。
- **对话循环由 harness 驱动**：`start_conversation` 取用户第一句 → Agent 调工具并回复 → `submit_assistant_message` 交给模拟用户，取回下一句 → 直到 runtime 返回结束原因、Agent 调用 `end_conversation`，或达到 `max_user_turns`。
- 模型只看到领域工具和 `end_conversation`；`configure_run`、`submit_*`、`record_termination` 等 harness 工具不暴露。
- **Baseline 刻意从简**：没有 prompt caching、上下文管理、重试、额外提示，system prompt 只有任务的 policy。

### 对上游任务的补丁（`arena_agents/patch_tau3_tasks.py`）

`prepare_tau3.sh` 生成任务后自动执行，可重复执行：

| 修改 | 原因 |
|---|---|
| runtime 容器与 verifier 增加 `AWS_BEARER_TOKEN_BEDROCK`、`AWS_REGION_NAME`、`TAU2_USER_LLM_ARGS_JSON` | 上游模板只转发 `OPENAI_*`，模拟用户和判定器无法使用 Bedrock |
| 两个镜像安装 `boto3` | LiteLLM 调 Bedrock 需要 |
| 两个镜像中的 tau2-bench 固定为 `v1.0.1` | 上游镜像克隆 main；tau2-bench 2.0.0 修改了评估接口，Harbor 0.24 的判定器会报 `missing ... 'communicate_scoring'` |
| `${OPENAI_API_KEY}` 等加空默认值 | 使用 Bedrock 时这些变量可以不设置 |

### 凭证

- 被测 Agent 在宿主机使用实例角色凭证调 Bedrock。
- 模拟用户和判定器在容器内运行，`run_tau3_baseline.sh` 在宿主机生成**短期 Bedrock API key**（默认 2 小时有效，只能调用 Bedrock）传入 runtime 容器和 verifier。AWS 凭证不会进入任何容器。
- 单次运行超过 2 小时（任务很多时），需要分批运行，或修改 `bedrock_token.py` 的 `--hours`。

## 6. 接入其他 Agent

**在 baseline 上叠加 harness 手段**（各队提交 Arena 的方式）：在 `arena/arena_agents/` 下新建模块，继承 `StrandsTau3Agent`，覆写下面的扩展点。工具转发和对话循环不用改。

| 扩展点 | 默认行为 | 可以做什么 |
|---|---|---|
| `build_system_prompt(policy)` | 简短角色说明 + 任务 policy | 补充做法、回复风格、完成标准 |
| `build_tools(environment, schemas)` | 领域工具 + `end_conversation` | 包装、过滤、合并工具；整形检索结果 |
| `build_agent(tools, system_prompt)` | `Agent(model, tools, system_prompt, SequentialToolExecutor)` | hooks、`conversation_manager`、plugins（skills 等） |
| `build_model()` | `BedrockModel(model_id, region, max_tokens)` | prompt caching 等模型参数（不能换模型） |

```python
# arena/arena_agents/team_a.py
from strands import Agent
from arena_agents.tau3_strands import StrandsTau3Agent

class TeamAgent(StrandsTau3Agent):
    def build_system_prompt(self, policy: str) -> str:
        return super().build_system_prompt(policy) + "\n回复简洁，不用 markdown。"

    def build_agent(self, tools, system_prompt) -> Agent:
        agent = super().build_agent(tools, system_prompt)
        # 例如：agent.hooks.add_hook(MyPolicyHook())
        return agent
```

```bash
AGENT=arena_agents.team_a:TeamAgent N_TASKS=10 ATTEMPTS=3 bash scripts/run_tau3_baseline.sh
```

**完全不同的 Agent**：继承 Harbor 的 `BaseAgent`，并且：

1. 声明 `capabilities = AgentCapabilities(mcp_servers=True)`；
2. `setup()` 中在 main 容器安装 `mcp==1.25.0` 并上传 `mcp_call.py`（可复用 `StrandsTau3Agent.setup` / `call_mcp`）；
3. `run()` 中用 `get_assistant_tool_schemas` 取工具定义，按 `start_conversation` → `submit_assistant_message` → 结束原因 的流程对话；
4. 在 `populate_context_post_run()` 中回填 token 与 metadata。

修改后运行离线测试：

```bash
cd arena && uv run pytest -q && uv run ruff check .
```

## 7. 常见问题

| 现象 | 原因与处理 |
|---|---|
| `Proceed? (Y/n)` 提示 | Harbor 确认要读取的环境变量；脚本已加 `-y`，手动运行时加 `-y` |
| `ModuleNotFoundError: No module named 'boto3'`（在 `exception.txt` 或 `mcp_calls.jsonl` 中） | 任务没有打补丁或是旧版本生成的，重新运行 `prepare_tau3.sh` |
| verifier `status: tau2_runtime_log_error`，`missing ... 'communicate_scoring'` | 镜像中的 tau2-bench 版本不对，`git pull` 后重新运行 `prepare_tau3.sh` |
| 生成 Bedrock token 失败，或容器内 `AccessDenied` / `UnrecognizedClient` | 宿主机角色缺少 Bedrock API key 相关权限（`bedrock:CallWithBearerToken`）；或改用 `USER_SIM=openai` |
| `compose build requires buildx 0.17.0 or later` | 重新运行 `setup_harbor.sh`，它会升级 Buildx |
| Linux 上提示已加入 docker 组 | 重新登录 SSH 后再运行 `setup_harbor.sh` |
| 下载 tau2-bench 很慢 | 脚本只检出所需目录（约 20 MB）；镜像构建时容器内仍会完整克隆一次，首次构建较慢，之后有缓存 |
| `no container found for service "main"` | 前面的镜像构建已失败，往上找第一处 build 错误 |

## 8. 验证状态（2026-11-06）

| 项目 | 状态 |
|---|---|
| `setup_harbor.sh`（Linux） | 实验环境通过，两个 oracle 任务 1.000 |
| `setup_harbor.sh`（macOS / Colima） | 未在真实环境运行 |
| 任务生成与补丁 | 本机生成 3 题验证；实验环境生成通过 |
| Agent 端到端（Opus 4.6 + Haiku 4.5 模拟用户） | task-001 完成对话，无异常 |
| 判定器（tau2-bench v1.0.1） | 修复后尚未在实验环境重跑确认 |
| baseline 通过率 | 尚无数据，下一步跑 `N_TASKS=10` |
