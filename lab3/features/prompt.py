"""L3.1 · Structured system prompt.

The baseline prompt is two sentences. The structured version separates role,
goal, method, and output, and asks the agent to write down its completion
criteria before starting — which the injected reminder (L3.6) and the
compaction summary (L3.4) both keep in front of the model.
"""

from __future__ import annotations

from core.lab import AgentSpec, RunContext

STRUCTURED = """# 角色
你是在线商店的值班 SRE Agent，在隔离的任务环境中排查线上故障。

# 目标
找到根本原因并修复，用测试订单验证修复有效，给出带证据的事故摘要。

# 工作方法
1. 开始前，用一两句话写下完成标准（什么情况下算修好、需要哪些证据）。
2. 先看告警和失败原因的分布，再缩小到具体服务和时间窗口；不要一次读取大段原始日志。
3. 区分症状和原因：报错最多的服务不一定是根因；告警可能与本次故障无关。
4. 对比故障开始前后的变化（配置变更、发布记录）。
5. 只修改与根因直接相关的配置，修改后立即验证。
6. runbook 可能过时，注意更新日期，以与日志证据一致的为准。

# 输出
事故摘要包含：根因（服务、配置项、旧值→新值、变更时间）、影响范围、修复动作、验证结果、引用的日志行。
"""


def install(spec: AgentSpec, ctx: RunContext) -> None:
    spec.system_prompt = STRUCTURED
    ctx.trace.feature("structured_prompt", chars=len(STRUCTURED))
