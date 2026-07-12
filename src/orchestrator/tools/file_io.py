"""File read/write tools, scoped to the per-task workspace directory."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from orchestrator.tools.base import ToolContext, ToolExecutionError, ToolSpec

_OWNERS = frozenset({"analysis", "writing", "code"})


def _resolve(workspace: Path, relative: str) -> Path:
    workspace = workspace.resolve()
    target = (workspace / relative).resolve()
    if not target.is_relative_to(workspace):
        raise ToolExecutionError(f"Path {relative!r} escapes the task workspace")
    return target


class FileReadInput(BaseModel):
    path: str = Field(min_length=1, description="Path relative to the task workspace")


class FileReadOutput(BaseModel):
    path: str
    content: str


class FileWriteInput(BaseModel):
    path: str = Field(min_length=1, description="Path relative to the task workspace")
    content: str


class FileWriteOutput(BaseModel):
    path: str
    bytes_written: int


def handle_read(args: FileReadInput, ctx: ToolContext) -> FileReadOutput:
    target = _resolve(ctx.workspace, args.path)
    if not target.is_file():
        raise ToolExecutionError(f"File {args.path!r} does not exist in the workspace")
    return FileReadOutput(path=args.path, content=target.read_text(encoding="utf-8"))


def handle_write(args: FileWriteInput, ctx: ToolContext) -> FileWriteOutput:
    target = _resolve(ctx.workspace, args.path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = args.content.encode("utf-8")
    target.write_bytes(data)
    return FileWriteOutput(path=args.path, bytes_written=len(data))


READ_SPEC = ToolSpec(
    name="file_read",
    description="Read a text file from the task workspace",
    input_schema=FileReadInput,
    output_schema=FileReadOutput,
    owners=_OWNERS,
    rate_limit=20,
    handler=handle_read,
)

WRITE_SPEC = ToolSpec(
    name="file_write",
    description="Write a text file into the task workspace",
    input_schema=FileWriteInput,
    output_schema=FileWriteOutput,
    owners=_OWNERS,
    rate_limit=20,
    handler=handle_write,
)
