# Strands 1.60 能力核查

对 strands-agents 1.60.0 源码的核查记录，供各 lab 实现时查阅。

| 能力 | Strands 组件 | 状态 |
|---|---|---|
| MCP 客户端 | `strands.tools.mcp.MCPClient` | 已核查；devopsagent 已有连接 Gateway 的示例 |
| 工具调用前拦截 | `BeforeToolCallEvent`：可写 `cancel_tool`、`selected_tool`、`tool_use`；支持 interrupt | 已核查 |
| 工具调用后改写 | `AfterToolCallEvent`：可写 `result`、`retry` | 已核查 |
| 模型调用后重试 | `AfterModelCallEvent`：可写 `retry` | 已核查 |
| 外层循环 | `AfterInvocationEvent.resume`：设置后 Agent 以该输入重新进入循环 | 已核查 |
| 完成判定循环 | `vended_plugins.goal.GoalLoop`：自然语言目标由独立 judge 判定，或传入程序化 validator | 已核查 |
| Skills | `vended_plugins.skills.AgentSkills`：SKILL.md + YAML frontmatter，元数据注入 system prompt，正文通过 `skills` 工具按需加载 | 已核查 |
| 大结果卸载 | `vended_plugins.context_offloader.ContextOffloader`：超阈值的工具结果存储后替换为预览和引用，可按行检索 | 已核查 |
| 每次调用临时注入上下文 | `vended_plugins.context_injector.ContextInjector`：不写入历史 | 已核查 |
| 对话压缩 | `SlidingWindowConversationManager`、`SummarizingConversationManager`；实验性 `ContextManager`（offload → summarize → 截断流水线） | 已核查 |
| Memory | `strands.memory.MemoryManager` + `vended_memory_stores.file_memory_store`：search/add 工具、自动注入、后台提取 | 已核查 |
| 人工审批 | `vended_interventions.hitl.HumanInTheLoop`（可选 LLM 风险分类器）；`vended_interventions.cedar`（策略） | 已核查 |
| Steering | `vended_plugins.steering`：Proceed / Guide / Interrupt | 已核查 |
| Sub-agent | `vended_tools.subagent`；Agent 作为工具；`multiagent.graph`、`multiagent.swarm` | 已核查 |
| 用量与轮数 | `AgentResult.metrics`（accumulated_usage、cycle_count）；`Limits(turns=...)` | devopsagent 已使用 |
| 工具结束循环 | 工具通过 `ToolContext` 设置 `invocation_state["request_state"]["stop_event_loop"] = True`（参考 `experimental.tools.stop`）。注意：模型直接回复文本也会结束循环，所以"必须调用 finish"要配合 `AfterInvocationEvent.resume` 实现 | 已核查 |
| 覆盖 MCP 工具描述 | `MCPClient` 只支持 `tool_filters`（allowed / rejected）和 `prefix`；改名、改描述在包装层实现 | 已核查 |
| 并行工具执行 | 默认 `ConcurrentToolExecutor`（端到端测试中已观察到并行）；可换 `SequentialToolExecutor` | 已核查 |
| Prompt caching | `BedrockModel(cache_config=CacheConfig(strategy="auto"))`，已接入 `context.cache`；cache token 统计待真实运行确认 | 部分核查 |
| 无人值守审批 | `HumanInTheLoop` 接受自定义 `ask` 回调，可以非交互运行；审批由谁决定见 LABS_SPEC.md Lab2 | 已核查 |

另一个实现约束：`Limits(turns=...)` 只限制单次调用，不跨 `resume` 累计。完成门控多轮 resume 时，总预算需要自己统计。

对"封装程度高"这一顾虑的结论：hook 系统开放了模型调用、工具调用、调用结束三个位置的读写，足以手写课程中所有机制。不方便的只有 ReAct 循环内部本身，这部分由 M1 的 native 教学循环覆盖。
