"""你的动态工作流：编排 Agent 生成工作流定义 → 执行引擎运行 → 完成检查 → 必要时重新规划。

下面给出一个建议的骨架，所有结构都可以修改。要求见 docs/lab.md。

记录事件让 chat 和 trace 能看到工作流在做什么：
    from labkit import trace
    trace.event("workflow", steps=[...])

你自己创建的 Agent（编排者、每个节点）要这样构造，token 才会按角色统计：
    Agent(model=build_model(label="节点名"), hooks=[trace.Tracer("节点名")], ...)
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from labkit import Env, build_model, trace  # noqa: F401  (you will need these)


# ---- 目标 1：工作流的表示 ---------------------------------------------------------------------
# 编排 Agent 输出这个结构（用 Strands 的 structured_output_model=Workflow）。
# 只用声明式数据，不要执行模型生成的代码。字段可以增删，例如并行度、过滤条件、完成标准。

class Step(BaseModel):
    id: str
    # tool：引擎直接调用一个查询工具取数据（不经过模型）；agent：一个子 Agent；foreach：对列表分批扇出
    kind: Literal["tool", "agent", "foreach"] = "agent"
    instruction: str = ""
    tool: str | None = None  # tool 步骤调用的工具
    arguments: dict[str, Any] = Field(default_factory=dict)
    tools: list[str] = Field(default_factory=list)  # 只给本步骤需要的工具
    depends_on: list[str] = Field(default_factory=list)
    output: Literal["text", "json"] = "text"
    items_from: str | None = None  # foreach 遍历哪个步骤的输出
    batch_size: int = 8


class Workflow(BaseModel):
    goal: str = ""
    steps: list[Step]


def validate(workflow: Workflow, available_tools: set[str], done: set[str]) -> list[str]:
    """执行前的静态检查，返回错误列表：id 重复、依赖不存在、循环依赖、未知工具、foreach 配置错误……"""
    raise NotImplementedError("TODO 目标 1")


# ---- 目标 2：执行引擎 -------------------------------------------------------------------------

class Engine:
    def run(self, workflow: Workflow, results: dict[str, Any]) -> Any:
        """按依赖执行所有步骤，互不依赖的步骤并行；把每一步的输出写进 results。

        失败时返回失败信息（哪一步、为什么），已完成步骤的结果保留在 results 中。
        """
        raise NotImplementedError("TODO 目标 2")

    def run_node(self, step: Step, prompt: str) -> str:
        """用一个上下文独立、只拿到 step.tools 的子 Agent 执行一个步骤。"""
        raise NotImplementedError("TODO 目标 2")


# ---- 目标 3、4：重新规划与完成检查 ---------------------------------------------------------------

def completion_problems(env: Env) -> list[str]:
    """对照 6 项要求做确定性检查（用 env.call 查询环境），返回未满足的项。"""
    raise NotImplementedError("TODO 目标 4")


class WorkflowRunner:
    def __init__(self, env: Env) -> None:
        self.env = env

    def __call__(self, task: str) -> str:
        """编排 → 校验 → 执行 → 完成检查；失败或检查不通过时，把情况交回编排 Agent 修改剩余工作流。"""
        raise NotImplementedError("TODO 目标 3")
