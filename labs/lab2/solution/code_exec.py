"""目标 3 · 在 Docker 沙箱里执行模型写的 Python 代码。

沙箱设计：
* 容器完全断网（--network none）、只读根文件系统、限制 CPU/内存/进程数/时间、非 root 用户。
* 代码通过 ``api.call(name, **args)`` 调用商店 API：请求经标准输入输出交给宿主机，
  宿主机只放行只读接口。所以不需要把环境服务暴露给容器网络，写操作也进不了代码。
* Docker 不可用时拒绝执行，绝不退回到本机直接运行。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

from strands import tool

from labkit import Env, trace

IMAGE = "python:3.12-slim"
TIMEOUT_SECONDS = 60
READ_ONLY_ENDPOINTS = {
    "payments_list_v2", "payments_get_v2", "payments_attempts_v2", "orders_list_v2", "orders_get_v2",
    "customers_get_v2", "approvals_get", "notifications_templates_v2",
}
RUNNER = Path(__file__).with_name("sandbox_runner.py")


class SandboxUnavailable(RuntimeError):
    pass


def docker_command(workdir: Path) -> list[str]:
    return [
        "docker", "run", "-i", "--rm",
        "--network", "none",
        "--read-only", "--tmpfs", "/tmp:rw,size=16m",
        "--memory", "256m", "--cpus", "0.5", "--pids-limit", "64",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--user", "65534:65534",
        "-v", f"{workdir}:/sandbox:ro",
        IMAGE, "python", "-I", "/sandbox/runner.py", "/sandbox/code.py",
    ]


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def run_code(code: str, env: Env, command: Any = docker_command) -> dict[str, Any]:
    """Run ``code`` in a fresh container, bridging ``api.call`` requests to the environment.

    ``command`` builds the container command; tests replace it, nothing else should.
    """
    if command is docker_command and not docker_available():
        raise SandboxUnavailable("Docker 不可用，拒绝执行代码（不会在本机直接运行）。")
    with tempfile.TemporaryDirectory(prefix="lab2-sandbox-") as tmp:
        workdir = Path(tmp)
        (workdir / "code.py").write_text(code, encoding="utf-8")
        shutil.copy(RUNNER, workdir / "runner.py")
        proc = subprocess.Popen(command(workdir), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        killer = threading.Timer(TIMEOUT_SECONDS, proc.kill)  # hard wall-clock limit
        killer.start()
        calls = 0
        try:
            for line in proc.stdout:  # one JSON message per line
                message = json.loads(line)
                if message["type"] == "done":
                    message["api_calls"] = calls
                    return message
                calls += 1
                proc.stdin.write(json.dumps(_bridge(env, message)) + "\n")
                proc.stdin.flush()
            error = proc.stderr.read()[-2000:] or f"沙箱退出（可能超过 {TIMEOUT_SECONDS} 秒被终止）"
            return {"type": "done", "error": error, "api_calls": calls}
        finally:
            killer.cancel()
            proc.kill()
            proc.wait()


def _bridge(env: Env, message: dict[str, Any]) -> dict[str, Any]:
    name = message.get("name", "")
    if name not in READ_ONLY_ENDPOINTS:
        trace.event("sandbox_denied", endpoint=name)
        return {"error": f"{name} 不允许在代码中调用：代码里只能调用只读接口，写操作请用普通工具。"}
    try:
        return {"result": env.call(name, **message.get("arguments", {}))}
    except RuntimeError as exc:
        return {"error": str(exc)}


def make_run_python(env: Env) -> Any:
    @tool
    def run_python(code: str) -> dict:
        """在隔离的沙箱中执行 Python 代码，适合批量翻页、汇总等需要很多次只读查询的工作。
        代码中用 api.call("接口名", **参数) 调用商店的只读接口（例如 payments_list_v2），
        把最终答案赋给变量 result。沙箱没有网络，也不能调用退款、通知等写接口。"""
        try:
            outcome = run_code(code, env)
        except SandboxUnavailable as exc:
            trace.event("sandbox_unavailable")
            return {"error": str(exc)}
        trace.event("sandbox_run", api_calls=outcome.get("api_calls"), ok="error" not in outcome)
        return {k: v for k, v in outcome.items() if k != "type"}

    return run_python
