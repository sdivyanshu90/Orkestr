"""Filesystem tools.

All paths are resolved relative to the task's worktree and are checked
against the task scope.  Symlinks that point outside the worktree are
rejected: without that check a repository could ship a symlink pointing at
``/etc`` and a write through it would escape the workspace.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..errors import ToolError
from ..security.capabilities import Capability
from ..security.policy import ToolRule
from .base import ToolContext, ToolResult

MAX_READ_BYTES = 512_000


class ReadFileTool:
    name = "read_file"
    rule = ToolRule(
        name="read_file",
        required_capabilities=(Capability.FS_READ,),
        path_argument="path",
        description="Read a UTF-8 text file from the task scope.",
    )

    async def invoke(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        raw = arguments.get("path")
        if not raw:
            return ToolResult(ok=False, error="path is required", error_class="validation")
        try:
            path = context.resolve(str(raw))
        except ToolError as exc:
            return ToolResult(ok=False, error=exc.message, error_class="policy")
        try:
            if path.is_symlink() and not path.resolve().is_relative_to(
                context.resolve(".")
            ):
                return ToolResult(
                    ok=False,
                    error="refusing to read through a symlink that escapes the workspace",
                    error_class="policy",
                )
            if not path.exists():
                return ToolResult(
                    ok=False, error=f"file not found: {raw}", error_class="validation"
                )
            if not path.is_file():
                return ToolResult(
                    ok=False, error=f"not a regular file: {raw}", error_class="validation"
                )
            data = path.read_bytes()[:MAX_READ_BYTES]
            return ToolResult(
                ok=True,
                content=data.decode("utf-8", errors="replace"),
                data={"path": str(raw), "bytes": len(data)},
            )
        except OSError as exc:
            return ToolResult(ok=False, error=str(exc), error_class="tool")


class WriteFileTool:
    name = "write_file"
    rule = ToolRule(
        name="write_file",
        required_capabilities=(Capability.FS_WRITE,),
        path_argument="path",
        description="Create or overwrite a UTF-8 text file inside the task scope.",
    )

    async def invoke(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        raw = arguments.get("path")
        if not raw:
            return ToolResult(ok=False, error="path is required", error_class="validation")
        content = arguments.get("content")
        if content is None:
            content = ""
        mode = str(arguments.get("mode") or "write")
        if mode not in ("write", "append"):
            return ToolResult(
                ok=False, error=f"unsupported mode {mode!r}", error_class="validation"
            )
        try:
            path = context.resolve(str(raw))
        except ToolError as exc:
            return ToolResult(ok=False, error=exc.message, error_class="policy")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if mode == "append":
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(str(content))
            else:
                tmp = path.with_name(path.name + ".squad-tmp")
                tmp.write_text(str(content), encoding="utf-8")
                os.replace(tmp, path)
            return ToolResult(
                ok=True,
                content=f"wrote {len(str(content))} bytes to {raw}",
                data={"path": str(raw), "bytes": len(str(content)), "mode": mode},
            )
        except (OSError, ValueError) as exc:
            return ToolResult(ok=False, error=str(exc), error_class="tool")


class ListDirTool:
    name = "list_dir"
    rule = ToolRule(
        name="list_dir",
        required_capabilities=(Capability.FS_READ,),
        path_argument="path",
        description="List the entries of a directory in the task scope.",
    )

    async def invoke(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        raw = str(arguments.get("path") or ".")
        try:
            path = context.resolve(raw)
        except ToolError as exc:
            return ToolResult(ok=False, error=exc.message, error_class="policy")
        try:
            if not path.exists():
                return ToolResult(
                    ok=False, error=f"directory not found: {raw}", error_class="validation"
                )
            if not path.is_dir():
                return ToolResult(
                    ok=False, error=f"not a directory: {raw}", error_class="validation"
                )
            entries = []
            for child in sorted(path.iterdir(), key=lambda p: p.name):
                entries.append(
                    {
                        "name": child.name,
                        "type": "dir" if child.is_dir() else "file",
                        "size": child.stat().st_size if child.is_file() else 0,
                    }
                )
            return ToolResult(
                ok=True,
                content="\n".join(f"{e['type']:>4} {e['size']:>8} {e['name']}" for e in entries),
                data={"path": raw, "entries": entries},
            )
        except OSError as exc:
            return ToolResult(ok=False, error=str(exc), error_class="tool")


class DeletePathTool:
    name = "delete_path"
    rule = ToolRule(
        name="delete_path",
        required_capabilities=(Capability.FS_DELETE,),
        needs_approval=True,
        approval_action="fs.delete",
        path_argument="path",
        description="Delete a path inside the task scope (requires human approval).",
    )

    async def invoke(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        raw = arguments.get("path")
        if not raw:
            return ToolResult(ok=False, error="path is required", error_class="validation")
        try:
            path = context.resolve(str(raw))
        except ToolError as exc:
            return ToolResult(ok=False, error=exc.message, error_class="policy")
        try:
            if path.is_dir():
                import shutil

                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
            else:
                return ToolResult(
                    ok=False, error=f"path not found: {raw}", error_class="validation"
                )
            return ToolResult(ok=True, content=f"deleted {raw}", data={"path": str(raw)})
        except OSError as exc:
            return ToolResult(ok=False, error=str(exc), error_class="tool")
