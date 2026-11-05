# Lab3 · Context engineering

## 场景

今晚 19:00 之后，部分用户 checkout 间歇性失败，涉及 gateway、checkout、inventory、payment、recommendation 五个服务。环境里有：

- 五个服务的日志，合计约 30 万行；
- 30 篇 runbook，其中有一篇是过时的；
- 服务配置的查询与修改，以及一个重放测试订单的工具。

环境的工具很"朴素"：`read_logs` 默认返回 500 行，`search_logs` 返回所有匹配行（最多 3000 行），`read_runbook` 返回整篇文档。所以上下文很容易被撑满。真实运行中，baseline 曾拉回约 36 万字符，模型调用直接报错 "prompt is too long"；SDK 默认的处理方式是删掉旧消息，结果 Agent 把刚查到的日志也丢了。

这个 lab 以多轮对话为主：你和 Agent 一起排查，并在过程中观察和控制上下文。

## 开始之前

```bash
uv sync
uv run python agent.py --task      # 跑一次 baseline
```

`--task` 结束时打印 `stats`（模型调用次数、输入 token、上下文峰值）和 `state`（是否修好、有没有改错配置、是否用大购物车验证过、读了哪些 runbook）。记进 `docs/record.md`。

chat 只内置了最少的命令：`/task`、`/state`、`/help`、`/reset`、`/quit`。其他以 `/` 开头的输入都会交给 `agent.py` 里的 `handle_command()`，由你来实现。

---

## 目标 1：slash command 机制，以及 `/context`

**要实现**：
- 在 `handle_command()` 里设计一个命令注册机制：每个命令有名字、说明、处理函数；`/help` 时列出你的命令。
- 第一个命令 `/context`：显示当前上下文的构成——system prompt、工具定义、用户消息、Agent 回复、工具调用、工具结果各占多少 token，以及占上下文窗口（200k）的比例。

**提示**：`agent.system_prompt`、`agent.tool_registry.get_all_tool_specs()`、`agent.messages` 里有你需要的一切。`labkit.trace.last_usage()` 返回上一次模型调用实际发送的输入 token，可以用来校对你的估算。

**验收**：在 chat 中让 Agent 拉取一段日志，`/context` 显示工具结果的占比明显上升。

**设计决策**：token 怎么估算（精确值只有调用之后才知道），以及估算和实际值差多少。

## 目标 2：`/compact`，手动和自动压缩

**要实现**：
- `/compact [保留重点]`：把较早的对话交给一个独立的摘要 Agent，按固定模板（任务目标、已确认的事实、已排除的假设、已执行的操作、下一步）写成摘要，替换历史消息，保留最近几轮原文。可选参数指定压缩时必须保留的内容，类似 Claude Code 的 `/compact`。
- 自动压缩：上下文超过阈值时自动触发。提示：继承 Strands 的 `ConversationManager`，在 `BeforeModelCallEvent` 中检查大小；上下文溢出时 SDK 会调用 `reduce_context()`。

**约束**：不能拆开工具调用（assistant 的 `toolUse`）和它的结果（下一条 user 的 `toolResult`）；消息的角色仍要 user / assistant 交替。

**验收**：排查到一半时执行 `/compact`，然后问"我们已经排除了哪些可能？下一步做什么？"，回答要和压缩前一致。

**观察**：压缩前后的消息数和 token（用 `/context`）；摘要内容；压缩后 Agent 是否重复已经做过的查询。

**设计决策**：摘要模板的字段，以及保留最近多少轮原文。

## 目标 3：大结果卸载

**要实现**：一个 `AfterToolCallEvent` hook：工具结果超过阈值时，完整内容写入文件，上下文里只留预览和文件路径；再提供按行读取、按关键词查找这个文件的工具。

**约束**：读取工具只能访问卸载目录下的文件。

**验收**：`--task` 不再出现 "prompt is too long"；`/context` 中工具结果的占比明显下降。

**观察**：每次卸载前后的长度；模型之后是否用读取工具找到了关键行。

**设计决策**：阈值多大；预览放什么（开头几行、统计摘要，还是匹配行）。

## 目标 4：按需检索，替代整段读取

**要实现**：
- 日志检索：按服务、级别、时间窗、关键词过滤，只返回匹配数、分布和少量样例；
- runbook 检索：本地检索（BM25 或关键词），返回最相关的段落，并附带文档的更新日期。提示：`export_runbooks` 可以一次拿到所有 runbook 来建索引。
- 隐藏整段读取的工具（`read_logs`、`search_logs`、`read_runbook`、`export_runbooks`）。

**验收**：Agent 不读过时的 runbook、不整段拉取日志，也能定位根因并修复。

**观察**：检索调用次数与返回长度；有没有被过时文档带偏。

**设计决策**：检索结果返回多少条、带哪些元数据。
