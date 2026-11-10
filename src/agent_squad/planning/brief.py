"""The task brief: the contract between the planner and a worker.

A brief is a *structured* description of what a worker must do.  It is not a
prompt: it is data that the worker renders into a prompt and that the runtime
can validate.  Keeping it structured means the orchestrator can check that a
worker was given a coherent task before spending a model call on it.

The brief is also what the deterministic mock provider consumes, which is why
the mock can produce genuine file changes rather than vague text.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..models.task import RepoScope, Role


class Edit(BaseModel):
    """A concrete file change requested by a task."""

    model_config = ConfigDict(extra="forbid")

    path: str
    content: str = ""
    mode: Literal["write", "append"] = "write"

    @model_validator(mode="after")
    def _validate(self) -> Edit:
        if not self.path.strip():
            raise ValueError("edit path must be non-empty")
        if self.path.startswith("/") or ".." in self.path.split("/"):
            raise ValueError(f"edit path must be relative and contained: {self.path!r}")
        return self


class TaskBrief(BaseModel):
    """Everything a worker needs to execute one task."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    role: Role
    objective: str
    description: str = ""
    workdir: str | None = None
    base_revision: str | None = None
    scope: RepoScope = Field(default_factory=RepoScope)
    acceptance_criteria: tuple[dict[str, Any], ...] = ()
    edits: tuple[Edit, ...] = ()
    commands: tuple[tuple[str, ...], ...] = ()
    notes: str = ""
    depends_on: tuple[str, ...] = ()
    context_refs: tuple[str, ...] = ()

    def to_prompt_block(self) -> str:
        """Render the brief for inclusion in a model prompt."""
        from ..providers.mock import BRIEF_END, BRIEF_START

        payload = self.model_dump(mode="json")
        return f"{BRIEF_START}\n{_dumps(payload)}\n{BRIEF_END}"


def _dumps(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, indent=2, sort_keys=True, default=str)
