# Lab 设计规范

| 项目 | 内容 |
|---|---|
| 版本 | 1.0（独立 starter 结构） |
| SDK | Strands Agents 1.60.0（核查记录见 `STRANDS_NOTES.md`）；MCP Python SDK 1.x（`FastMCP`） |
| 模型 | Bedrock，默认 `global.anthropic.claude-haiku-4-5-20251001-v1:0`（`us-west-2`），可用 `MODEL_ID` / `AWS_REGION` 修改 |
| 实验目标 | `LAB_GOALS.md` |
| 接入协议 | `AGENT_PROTOCOL.md`（Command Adapter v0.2） |
| 历史版本 | 基于 profile 开关的旧实现（`core/`、`lab1`–`lab5`）保存在 git tag `v0-profile-labs` |

## 1. 设计原则

1. **每个 lab 是一个独立项目**，学员 clone 后 `uv sync` 即可运行，不依赖仓库里的其他目录。
2. **学员只编辑 `agent.py`**（以及自己新建的模块），里面是原生的 Strands 写法：`Agent(model=..., tools=[...], hooks=[...])`。实验基础设施放在 `labkit/`，学员不需要阅读。
3. **目标用自然语言描述**，每个目标写清要实现什么、约束、验收、观察点和一个设计决策（`docs/lab.md`）。学员可以使用实验环境里的 code agent；`AGENTS.md` 告诉 code agent 项目结构和不许修改的文件。
4. **参考答案可以直接运行**（`solution/`），用 `--mode` 切换各目标对应的实现，方便与 baseline 对比。
5. **lab 不评分**。`--task` 结束时打印环境的最终状态（`/state`），只描述发生了什么；评分统一在 Arena 中进行。

## 2. 单个 lab 的结构

```text
labs/labN/
├── agent.py              # 学员编辑：build_agent() 等扩展点
├── docs/lab.md           # 实验目标
├── docs/record.md        # 对照记录表
├── AGENTS.md             # 给 code agent 的说明
├── README.md
├── labkit/               # 环境、chat CLI、trace、Arena 入口（学员不需要读）
│   ├── env.py            # Env：本地启动场景，或连接 Arena 的 MCP Gateway
│   ├── server.py         # 本地场景的 MCP server（FastMCP，只绑定 127.0.0.1，带单次运行 token）
│   ├── scenario.py       # 场景数据与原始 API
│   ├── trace.py          # 模型调用、工具调用、自定义事件 → reports/trace-<时间>.jsonl
│   ├── model.py          # build_model()
│   ├── cli.py            # chat / -p / --task
│   └── eval_entry.py     # Command Adapter v0.2 入口
├── solution/             # 参考答案
├── agent_eval_entry.py   # Arena 入口（--mode，--solution 运行参考答案）
├── agent.toml            # 协议描述文件
├── tests/                # 脚本化模型测试，不调用 Bedrock
├── pyproject.toml / uv.lock
```

`labkit/` 在四个 lab 中由同一份代码复制并按场景改造，各自独立演进。

## 3. 运行方式

```bash
uv sync
uv run python agent.py                    # 对话；/task 发送场景任务，/help 查看命令
uv run python agent.py --task             # 跑一次任务，打印 stats 与 state
uv run python agent.py -p "问题"           # 问一个问题后退出
uv run python -m solution.agent --mode full --task   # 运行参考答案
uv run pytest -q
```

对话模式内置的命令随 lab 不同而不同；Lab3 起，学员自己实现的命令通过 `agent.py` 中的扩展点接入。

## 4. 各 lab 的场景与扩展点

| Lab | 场景 | 学员扩展点 | 参考答案的模式 |
|---|---|---|---|
| lab2 · Tools 与 MCP | 重复扣款对账：17 个原始 API，分页、冗长返回、含糊错误码、废弃接口、订单备注中的注入 | `build_tools()`、`build_hooks()`、`build_agent()` | `baseline` `tool` `mcp` `code` `governance` `full` |
| lab3 · Context engineering | checkout 间歇失败：5 个服务约 30 万行日志、30 篇 runbook（含一篇过时文档） | `handle_command()`（实现 `/context`、`/compact`）、`build_tools()`、`build_hooks()`、`build_agent()`（`conversation_manager`） | `baseline` `compact` `offload` `retrieval` `full` |
| lab4 · Skills 与长期记忆 | 连续处理 10 张工单，每张一个新会话；三份 SOP；跨工单事实、客户偏好、质检反馈 | `build_agent(env, mode, session)`（每张工单调用一次）、`on_session_end()`（拿到本工单轨迹）、`COMMANDS` | `baseline` `sop_in_prompt` `skills` `memory` `full` |
| lab5 · 动态工作流 | 大促事故处理：6 项要求、约 40 个订单、重放接口中途 503、部分库存售罄 | `build_agent()` 返回 Agent 或工作流运行器（`__call__`、`cleanup`）；`workflow.py` 骨架 | `baseline` `workflow` |

## 5. 实现约定

- **环境访问**：原始 API 由 `env.mcp_client(**kwargs)` 提供，直接放进 `tools`（Strands 的 `MCPClient` 是 ToolProvider，生命周期由 Agent 管理）；在代码里调用用 `env.call(name, **args)`。
- **错误信息**：环境 server 把 API 的错误码原样传给模型（例如 `ERR_5031` 与 `ERR_4012`），否则模型无法区分暂时故障和业务错误。
- **追踪**：`labkit` 自动给 Agent 挂上 `trace.Tracer`；学员用 `trace.event(name, **fields)` 记录自己的事件，chat 中实时显示。辅助模型用 `build_model(label=...)` 区分统计。
- **安全**：本地场景 server 只绑定 `127.0.0.1` 并校验单次运行 token；Lab2 的代码执行只允许在 Docker 沙箱中进行（断网、只读、资源限制），Docker 不可用时拒绝执行。
- **状态隔离**：Lab4 的 `memory/`、`skills/` 声明为 Arena 的 `snapshot_paths`，每次运行使用独立副本。

## 6. 验证状态

| Lab | 测试 | 真实运行（参考答案） | 未验证 |
|---|---|---|---|
| lab2 | 11 | tool / mcp / governance / full | 目标 3 的沙箱需在有 Docker 的环境中验证 |
| lab3 | 11 | baseline / full；chat 中的 `/compact` | compact、offload、retrieval 单独模式 |
| lab4 | 13 | baseline / full（3 次） | skills、memory 单独模式；第 6、9 张工单的记忆抽取不稳定 |
| lab5 | 25 | baseline / workflow（5 次迭代） | 工单守卫（TicketGuard）尚未用真实模型验证 |

运行数据见 `OBSERVATIONS.md`。
