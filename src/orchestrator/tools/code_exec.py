"""Sandboxed Python code execution.

Default backend runs the snippet in a dedicated minimal Docker container with
no network and CPU/memory/time limits (docker/sandbox.Dockerfile). The
`subprocess` backend (isolated-mode interpreter with a wall-clock timeout) is
for environments without Docker — used by the test suite.
"""

from __future__ import annotations

import subprocess
import sys

from pydantic import BaseModel, Field

from orchestrator.config import get_settings
from orchestrator.tools.base import ToolContext, ToolExecutionError, ToolSpec

_TRUNCATE = 10_000


class CodeExecInput(BaseModel):
    code: str = Field(min_length=1, description="Python source to execute")
    timeout_s: int | None = Field(default=None, ge=1, le=60)


class CodeExecOutput(BaseModel):
    stdout: str
    stderr: str
    exit_code: int


def _run(command: list[str], code: str, timeout: int, cwd=None) -> CodeExecOutput:
    try:
        proc = subprocess.run(
            command, input=code, capture_output=True, text=True, timeout=timeout, cwd=cwd
        )
    except subprocess.TimeoutExpired:
        raise ToolExecutionError(f"Code execution timed out after {timeout}s") from None
    except FileNotFoundError as exc:
        raise ToolExecutionError(f"Execution backend unavailable: {exc}") from exc
    return CodeExecOutput(
        stdout=proc.stdout[:_TRUNCATE], stderr=proc.stderr[:_TRUNCATE], exit_code=proc.returncode
    )


def handle(args: CodeExecInput, ctx: ToolContext) -> CodeExecOutput:
    settings = get_settings()
    timeout = args.timeout_s or settings.code_exec_timeout_s
    if settings.code_exec_backend == "docker":
        command = [
            "docker", "run", "--rm", "-i",
            "--network", "none",
            "--memory", "256m",
            "--cpus", "0.5",
            "--pids-limit", "64",
            settings.code_exec_image,
            "python", "-I", "-",
        ]
        return _run(command, args.code, timeout)
    # subprocess backend: isolated interpreter, wall-clock timeout, workspace cwd.
    ctx.workspace.mkdir(parents=True, exist_ok=True)
    return _run([sys.executable, "-I", "-"], args.code, timeout, cwd=ctx.workspace)


SPEC = ToolSpec(
    name="code_exec",
    description="Execute a Python snippet in a sandbox and return stdout/stderr",
    input_schema=CodeExecInput,
    output_schema=CodeExecOutput,
    owners=frozenset({"analysis", "code"}),
    rate_limit=5,
    handler=handle,
)
