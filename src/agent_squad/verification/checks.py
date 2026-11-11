"""Deterministic acceptance check executors.

Each check is a small, self-contained coroutine that either returns a
:class:`~agent_squad.models.evidence.CheckRecord` describing exactly what was
run and what happened, or raises.  The records are the *evidence*: they say
"this command was executed at this time against this workspace and exited
with this code", which is a recorded result -- not a proof of correctness.

Advisory criteria (``llm_review``, ``manual``) have no deterministic executor
here.  They are reported as advisory, never as passed.
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path

from ..config import SandboxConfig
from ..errors import ToolError
from ..models.evidence import CheckRecord
from ..models.task import AcceptanceCriterion, CheckKind, utcnow
from ..sandbox.limits import Sandbox


class CheckOutcome:
    """The result of running one criterion's check."""

    def __init__(self, record: CheckRecord) -> None:
        self.record = record

    @property
    def passed(self) -> bool:
        return self.record.passed


def _record(
    criterion: AcceptanceCriterion,
    *,
    command: tuple[str, ...] = (),
    exit_code: int | None = None,
    passed: bool = False,
    predicates: dict[str, bool] | None = None,
    stdout: str = "",
    stderr: str = "",
    duration_s: float = 0.0,
    started_at: _dt.datetime | None = None,
    error: str | None = None,
    deterministic: bool = True,
) -> CheckRecord:
    """Build a check record.

    ``passed`` is the complete verdict.  ``exit_code`` is recorded for
    diagnosis but is never the verdict on its own: a command that exits zero
    with the wrong output has failed.
    """
    return CheckRecord(
        criterion_id=criterion.id,
        kind=criterion.kind.value,
        command=command,
        exit_code=exit_code,
        passed=passed,
        predicates=dict(predicates or {}),
        stdout_excerpt=stdout[-4000:],
        stderr_excerpt=stderr[-4000:],
        duration_s=duration_s,
        started_at=started_at,
        finished_at=utcnow(),
        error=error,
        deterministic=deterministic,
    )


async def run_criterion(
    criterion: AcceptanceCriterion,
    *,
    workspace: Path,
    sandbox_config: SandboxConfig,
    has_changes: bool = False,
    timeout_s: float = 600.0,
) -> CheckOutcome:
    """Execute the check for one acceptance criterion."""
    kind = criterion.kind
    spec = criterion.spec

    if kind in (CheckKind.LLM_REVIEW, CheckKind.MANUAL):
        return CheckOutcome(
            _record(
                criterion,
                error="advisory criterion: no deterministic executor",
                deterministic=False,
            )
        )

    if kind == CheckKind.FILE_EXISTS:
        target = _resolve(workspace, str(spec["path"]))
        exists = target.exists()
        return CheckOutcome(
            _record(
                criterion,
                exit_code=0 if exists else 1,
                passed=exists,
                predicates={"exists": exists},
                stdout=f"{spec['path']} exists: {exists}",
                error=None if exists else f"file not found: {spec['path']}",
            )
        )

    if kind in (CheckKind.FILE_CONTAINS, CheckKind.FILE_NOT_CONTAINS):
        target = _resolve(workspace, str(spec["path"]))
        if not target.exists():
            return CheckOutcome(
                _record(criterion, exit_code=1, error=f"file not found: {spec['path']}")
            )
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return CheckOutcome(_record(criterion, exit_code=1, error=str(exc)))
        pattern = str(spec["pattern"])
        regex = bool(spec.get("regex", False))
        try:
            found = (
                re.search(pattern, content) is not None if regex else pattern in content
            )
        except re.error as exc:
            return CheckOutcome(_record(criterion, exit_code=1, error=f"bad pattern: {exc}"))
        wants_contains = kind == CheckKind.FILE_CONTAINS
        ok = found == wants_contains
        return CheckOutcome(
            _record(
                criterion,
                exit_code=0 if ok else 1,
                passed=ok,
                predicates={"pattern_matched": found, "expected_presence": wants_contains},
                stdout=f"pattern {pattern!r} present={found}",
                error=None if ok else f"pattern {pattern!r} present={found}",
            )
        )

    if kind == CheckKind.DIFF_NONEMPTY:
        ok = bool(has_changes)
        return CheckOutcome(
            _record(
                criterion,
                exit_code=0 if ok else 1,
                passed=ok,
                predicates={"changes_present": has_changes},
                stdout=f"changes present: {has_changes}",
                error=None if ok else "no changes were produced",
            )
        )

    if kind == CheckKind.DIFF_EMPTY:
        ok = not has_changes
        return CheckOutcome(
            _record(
                criterion,
                exit_code=0 if ok else 1,
                passed=ok,
                predicates={"changes_present": has_changes},
                stdout=f"changes present: {has_changes}",
                error=None if ok else "unexpected changes were produced",
            )
        )

    if kind in (
        CheckKind.COMMAND_EXITS_ZERO,
        CheckKind.COMMAND_OUTPUT_MATCHES,
        CheckKind.PYTEST_SELECTOR,
    ):
        command = _command_for(criterion)
        return await _run_command(
            criterion, command, workspace, sandbox_config, timeout_s
        )

    return CheckOutcome(
        _record(criterion, exit_code=1, error=f"unsupported check kind {kind.value}")
    )


def _command_for(criterion: AcceptanceCriterion) -> tuple[str, ...]:
    kind = criterion.kind
    spec = criterion.spec
    if kind == CheckKind.PYTEST_SELECTOR:
        command = [
            str(part) for part in spec.get("command", ["python3", "-m", "pytest", "-q"])
        ]
        selector = str(spec["selector"])
        return tuple([*command, selector])
    return tuple(str(part) for part in spec["command"])


async def _run_command(
    criterion: AcceptanceCriterion,
    command: tuple[str, ...],
    workspace: Path,
    sandbox_config: SandboxConfig,
    timeout_s: float,
) -> CheckOutcome:
    sandbox = Sandbox(sandbox_config)
    started = utcnow()
    try:
        result = await sandbox.run(list(command), cwd=workspace, timeout_s=timeout_s)
    except ToolError as exc:
        return CheckOutcome(_record(criterion, exit_code=None, error=exc.message))
    stdout = result.stdout
    stderr = result.stderr
    if criterion.kind == CheckKind.COMMAND_OUTPUT_MATCHES:
        pattern = str(criterion.spec["pattern"])
        regex = bool(criterion.spec.get("regex", False))
        try:
            found = (
                re.search(pattern, stdout, re.MULTILINE) is not None
                if regex
                else pattern in stdout
            )
        except re.error as exc:
            return CheckOutcome(
                _record(
                    criterion, exit_code=result.exit_code, error=f"bad pattern: {exc}"
                )
            )
        ok = found and result.exit_code == 0
        return CheckOutcome(
            _record(
                criterion,
                command=command,
                exit_code=result.exit_code,
                passed=ok,
                predicates={
                    "pattern_matched": found,
                    "exit_code_zero": result.exit_code == 0,
                },
                stdout=stdout,
                stderr=stderr,
                duration_s=result.duration_s,
                started_at=started,
                error=None
                if ok
                else (
                    f"pattern {pattern!r} found={found} "
                    f"(exit code {result.exit_code})"
                ),
            )
        )
    ok = result.exit_code == 0 and not result.timed_out
    return CheckOutcome(
        _record(
            criterion,
            command=command,
            exit_code=result.exit_code,
            passed=ok,
            predicates={
                "exit_code_zero": result.exit_code == 0,
                "not_timed_out": not result.timed_out,
            },
            stdout=stdout,
            stderr=stderr,
            duration_s=result.duration_s,
            started_at=started,
            error=None
            if ok
            else ("command timed out" if result.timed_out else f"exit code {result.exit_code}"),
        )
    )


def _resolve(workspace: Path, relative: str) -> Path:
    candidate = (workspace / relative).resolve()
    root = workspace.resolve()
    if candidate != root and root not in candidate.parents:
        raise ToolError(f"path {relative!r} escapes the verification workspace")
    return candidate
