"""Workflow definition and a small deterministic execution engine.

The orchestrator (an LLM) writes a *declarative* JSON workflow; this engine runs it.
Nothing the model writes is executed as code.

Primitives, expressed through three step kinds plus dependencies:

* ``tool``    — the engine calls one environment tool itself (following pagination)
  and keeps the result as data. No model involved, so lists are never truncated or
  re-typed by an LLM;
* ``agent``   — one sub-agent with a fresh context and only the listed tools;
* ``foreach`` — fan-out over a list produced by an earlier step (``where`` filters,
  ``unique_by`` dedups). With ``instruction``: one sub-agent per batch, in parallel.
  With ``tool``: the engine calls the tool once per item, substituting ``{field}``
  placeholders in ``arguments`` — deterministic, no model;
* ``depends_on`` — a chain of steps is a pipeline; steps whose dependencies are all
  satisfied run in parallel.

Steps pass data *by reference*: a ``foreach`` names the step whose output it
iterates, and each step sees only compact summaries of its dependencies. The
orchestrator never receives raw per-order data, so its context does not grow with
the number of orders.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any, Literal

from pydantic import BaseModel, Field

from labkit import trace


class Step(BaseModel):
    id: str = Field(description="唯一标识，小写字母、数字和下划线")
    kind: Literal["tool", "agent", "foreach"] = Field(
        "agent", description="tool：引擎直接调用一个查询工具取数据；agent：单个子 Agent；foreach：对列表分批扇出")
    instruction: str = Field("", description="交给子 Agent 的指令，写清要做什么和要返回什么（tool 步骤不需要）")
    tool: str | None = Field(None, description="tool 步骤调用的工具名；foreach 设置 tool 时对每个条目调用一次")
    arguments: dict[str, Any] = Field(default_factory=dict,
                                      description="工具参数；foreach 中可用 {字段名} 引用条目字段，例如 {\"order_id\": \"{id}\"}")
    paginate: bool = Field(False, description="tool 步骤自动翻页（page 递增，直到 has_more 为 false）")
    tools: list[str] = Field(default_factory=list, description="该步骤可用的环境工具（只给需要的）")
    depends_on: list[str] = Field(default_factory=list, description="必须先完成的步骤 id")
    output: Literal["text", "json"] = Field("text", description="json：要求返回 JSON（后续 foreach 要用就必须是 json）")
    items_from: str | None = Field(None, description="foreach 遍历的列表来自哪个步骤（必须在 depends_on 中）")
    where: dict[str, str] | None = Field(None, description="foreach 只处理字段值匹配的条目，例如 {\"status\": \"failed\"}")
    unique_by: str | None = Field(None, description="foreach 按该字段去重后再处理，例如 customer_id")
    batch_size: int = Field(8, ge=1, le=20, description="foreach 每批条目数")


class Workflow(BaseModel):
    goal: str = Field("", description="这个工作流要达成什么")
    steps: list[Step]


class StepFailed(RuntimeError):
    def __init__(self, step: str, reason: str) -> None:
        super().__init__(f"{step}: {reason}")
        self.step, self.reason = step, reason


NodeRunner = Callable[[Step, str, str], str]  # (step, label, prompt) -> reply text
ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
ToolCaller = Callable[..., Any]  # call(name, **arguments) -> decoded result


# ---- validation ----------------------------------------------------------------------

def normalize(workflow: Workflow) -> Workflow:
    """Fix harmless omissions instead of rejecting: a step that feeds a foreach must output JSON."""
    feeds = {s.items_from for s in workflow.steps if s.kind == "foreach"}
    for step in workflow.steps:
        if step.id in feeds and step.kind == "agent":
            step.output = "json"
    return workflow


def validate(workflow: Workflow, available_tools: set[str], done: set[str] = frozenset()) -> list[str]:
    """Static checks before anything runs. ``done``: ids of steps completed in earlier rounds."""
    errors: list[str] = []
    ids = [s.id for s in workflow.steps]
    known = set(ids) | set(done)
    for step in workflow.steps:
        if not ID_PATTERN.match(step.id):
            errors.append(f"{step.id}: id 只能用小写字母、数字和下划线")
        if ids.count(step.id) > 1 or step.id in done:
            errors.append(f"{step.id}: id 重复（或与已完成的步骤重名）")
        for dep in step.depends_on:
            if dep not in known:
                errors.append(f"{step.id}: 依赖的步骤 {dep} 不存在")
        unknown = set(step.tools) - available_tools
        if unknown:
            errors.append(f"{step.id}: 未知工具 {sorted(unknown)}")
        if step.kind == "tool" and step.tool not in available_tools:
            errors.append(f"{step.id}: tool 步骤必须指定一个可用的工具（tool={step.tool!r}）")
        if step.kind == "foreach" and step.tool and step.tool not in available_tools:
            errors.append(f"{step.id}: 未知工具 {step.tool!r}")
        if step.kind == "agent" and not step.instruction.strip():
            errors.append(f"{step.id}: 缺少 instruction")
        if step.kind == "foreach" and not (step.instruction.strip() or step.tool):
            errors.append(f"{step.id}: foreach 需要 instruction（交给子 Agent）或 tool（逐条调用工具）")
        if step.kind == "foreach":
            if not step.items_from or step.items_from not in step.depends_on:
                errors.append(f"{step.id}: foreach 必须设置 items_from，且它要在 depends_on 中")
            source = next((s for s in workflow.steps if s.id == step.items_from), None)
            if source is not None and source.kind != "tool" and source.output != "json":
                errors.append(f"{step.id}: items_from 指向的 {source.id} 必须是 tool 步骤或 output=json")
    if not errors and _has_cycle(workflow):
        errors.append("步骤之间存在循环依赖")
    return errors


def _has_cycle(workflow: Workflow) -> bool:
    graph = {s.id: [d for d in s.depends_on if d in {x.id for x in workflow.steps}] for s in workflow.steps}
    state: dict[str, int] = {}

    def visit(node: str) -> bool:
        if state.get(node) == 1:
            return True
        if state.get(node) == 2:
            return False
        state[node] = 1
        if any(visit(d) for d in graph[node]):
            return True
        state[node] = 2
        return False

    return any(visit(n) for n in graph)


# ---- execution ------------------------------------------------------------------------

class Engine:
    def __init__(self, run_node: NodeRunner, call_tool: ToolCaller | None = None, max_workers: int = 4,
                 context_chars: int = 4000) -> None:
        self.run_node = run_node
        self.call_tool = call_tool
        self.max_workers = max_workers
        self.context_chars = context_chars

    def run(self, workflow: Workflow, results: dict[str, Any]) -> StepFailed | None:
        """Run every step, filling ``results`` (step id -> output). Returns the first failure, if any.

        On failure no new steps start; steps already running finish and keep their results,
        so the orchestrator can re-plan only what is left.
        """
        pending = {s.id: s for s in workflow.steps}
        failure: StepFailed | None = None
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            running: dict[Any, Step] = {}
            while (pending and failure is None) or running:
                if failure is None:
                    for step in [s for s in pending.values() if all(d in results for d in s.depends_on)]:
                        del pending[step.id]
                        trace.event("step_start", step=step.id, kind=step.kind, depends_on=step.depends_on)
                        running[pool.submit(self._execute, step, dict(results))] = step
                if not running:
                    break  # unreachable after validation: nothing can start
                finished, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in finished:
                    step = running.pop(future)
                    try:
                        results[step.id] = future.result()
                        trace.event("step_done", step=step.id, output=summarize(results[step.id], 200))
                    except StepFailed as exc:
                        trace.event("step_failed", step=step.id, reason=exc.reason)
                        failure = failure or exc
                    except Exception as exc:  # a crashed node is a failed step
                        trace.event("step_failed", step=step.id, reason=repr(exc))
                        failure = failure or StepFailed(step.id, repr(exc))
        return failure

    def _execute(self, step: Step, results: dict[str, Any]) -> Any:
        if step.kind == "tool":
            return self._run_tool(step)
        context = self._dependency_context(step, results)
        if step.kind == "agent":
            suffix = "\n\n最后的回复只输出一个 JSON 值（对象或数组），不要输出其他文字。" if step.output == "json" else ""
            return self._parse(step, self.run_node(step, step.id, step.instruction + suffix + context))
        items = _as_list(results.get(step.items_from))
        if items is None:
            raise StepFailed(step.id, f"{step.items_from} 的输出不是列表，无法 foreach")
        if step.where:
            source_count, fields = len(items), sorted({k for i in items[:5] if isinstance(i, dict) for k in i})
            items = [i for i in items if isinstance(i, dict) and all(str(i.get(k)) == v for k, v in step.where.items())]
            if source_count and not items:  # a silent empty fan-out hides a wrong field name
                raise StepFailed(step.id, f"where={step.where} 过滤后没有条目；{step.items_from} 的条目字段为 {fields}")
        if step.unique_by:
            seen: set[Any] = set()
            items = [i for i in items if isinstance(i, dict) and not (i.get(step.unique_by) in seen
                                                                       or seen.add(i.get(step.unique_by)))]
        if not items:
            return []
        if step.tool:
            return self._tool_per_item(step, items)
        batches = [items[i:i + step.batch_size] for i in range(0, len(items), step.batch_size)]
        trace.event("foreach", step=step.id, items=len(items), batches=len(batches))
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            replies = list(pool.map(
                lambda ib: self.run_node(step, f"{step.id}#{ib[0] + 1}", _batch_prompt(step, ib[1], context)),
                enumerate(batches)))
        rows: list[Any] = []
        for batch, reply in zip(batches, replies, strict=True):
            _check_failed(step, reply)
            parsed = _json_rows(reply)
            rows.extend(parsed if parsed else [{"item": item, "status": "unreported"} for item in batch])
        return rows

    def _tool_per_item(self, step: Step, items: list[Any]) -> list[dict[str, Any]]:
        """Deterministic fan-out: one tool call per item. Per-item errors are kept as rows."""
        if self.call_tool is None:
            raise StepFailed(step.id, "引擎没有配置工具调用")
        rows = []
        for item in items:
            try:
                arguments = {k: _fill(v, item) for k, v in step.arguments.items()}
                rows.append({"item": item, "status": "ok", "result": self.call_tool(step.tool, **arguments)})
            except (KeyError, RuntimeError) as exc:
                rows.append({"item": item, "status": "error", "error": str(exc)[:300]})
        failed = [r for r in rows if r["status"] == "error"]
        trace.event("tool_foreach", step=step.id, tool=step.tool, items=len(items), errors=len(failed))
        if failed and len(failed) == len(rows):  # every call failed: almost always a wrong placeholder
            fields = sorted(items[0]) if isinstance(items[0], dict) else []
            raise StepFailed(step.id, f"所有条目的 {step.tool} 调用都失败了，例如：{failed[0]['error']}；"
                                      f"条目字段为 {fields}，检查 arguments 中的占位符")
        return rows

    def _run_tool(self, step: Step) -> Any:
        if self.call_tool is None:
            raise StepFailed(step.id, "引擎没有配置工具调用")
        try:
            if not step.paginate:
                return self.call_tool(step.tool, **step.arguments)
            rows: list[Any] = []
            for page in range(1, 101):
                result = self.call_tool(step.tool, **{**step.arguments, "page": page})
                rows.extend(_as_list(result) or [])
                if not (isinstance(result, dict) and result.get("has_more")):
                    break
            trace.event("tool_step", step=step.id, tool=step.tool, rows=len(rows))
            return rows
        except RuntimeError as exc:
            raise StepFailed(step.id, str(exc)[:500]) from exc

    def _parse(self, step: Step, reply: str) -> Any:
        _check_failed(step, reply)
        if step.output == "text":
            return reply
        value = _first_json(reply)
        if value is None:
            raise StepFailed(step.id, "要求返回 JSON，但输出中没有可解析的 JSON")
        return value

    def _dependency_context(self, step: Step, results: dict[str, Any]) -> str:
        if not step.depends_on:
            return ""
        parts = [f"### {dep}\n{summarize(results[dep], self.context_chars)}" for dep in step.depends_on]
        return "\n\n## 前置步骤的结果\n" + "\n\n".join(parts)


def summarize(value: Any, limit: int) -> str:
    """Compact view of a step output: lists show counts per status/outcome plus a sample."""
    if isinstance(value, list):
        counts: dict[str, int] = {}
        for row in value:
            key = str(row.get("outcome") or row.get("status") or "item") if isinstance(row, dict) else "item"
            counts[key] = counts.get(key, 0) + 1
        text = json.dumps({"count": len(value), "by_outcome": counts}, ensure_ascii=False)
        sample = json.dumps(value, ensure_ascii=False)
        return (text + "\n" + sample)[:limit]
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


def _fill(value: Any, item: Any) -> Any:
    """Replace ``{field}`` placeholders with the item's fields; a lone placeholder keeps the type."""
    if not isinstance(value, str) or not isinstance(item, dict):
        return value
    whole = re.fullmatch(r"\{(?:item\.)?(\w+)\}", value)
    if whole:
        return item[whole.group(1)]
    return re.sub(r"\{(?:item\.)?(\w+)\}", lambda m: str(item[m.group(1)]), value)


def _batch_prompt(step: Step, batch: list[Any], context: str) -> str:
    return (f"{step.instruction}\n\n## 本批条目（共 {len(batch)} 条）\n{json.dumps(batch, ensure_ascii=False)}\n\n"
            "处理完后，每个条目输出一行 JSON 对象（包含条目标识和处理结果 outcome），不要输出其他内容。" + context)


def _check_failed(step: Step, reply: str) -> None:
    head = reply.strip().splitlines()[0] if reply.strip() else ""
    if head.upper().startswith("FAILED"):
        raise StepFailed(step.id, reply.strip()[:500])


def _as_list(value: Any) -> list[Any] | None:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        lists = {k: v for k, v in value.items() if isinstance(v, list)}
        if len(lists) == 1:
            return next(iter(lists.values()))
        for key in ("orders", "items", "data", "rows", "results", "tickets", "customers"):
            if key in lists:
                return lists[key]
    return None


def _first_json(text: str) -> Any:
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    candidates = [fenced.group(1)] if fenced else []
    candidates += [text[i:] for i, ch in enumerate(text) if ch in "[{"][:5]
    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            return decoder.raw_decode(candidate.strip())[0]
        except json.JSONDecodeError:
            continue
    return None


def _json_rows(text: str) -> list[dict[str, Any]]:
    whole = _first_json(text)
    if isinstance(whole, list):
        return [r for r in whole if isinstance(r, dict)]
    rows = []
    for line in text.splitlines():
        line = line.strip().strip(",")
        if line.startswith("{"):
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows
