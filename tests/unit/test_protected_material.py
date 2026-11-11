"""C4: the protected verification material is outside the worker's authority.

The oracle that judges the work must not be the worker's to change.  These
tests exercise the **real worker execution paths** -- the file tools and
the ``run_command`` tool -- rather than helper functions, because the
question is not "does the hash compare correctly" but "can a worker, while
it is running, delete, rewrite or restore the thing that judges it?".

The distinction the policy draws is the important one:

* a hash comparison **after** the run cannot see a worker that rewrote the
  material, ran the check and put the original back;
* a refusal **during** the run stops the change from happening at all.

So the tests here assert refusals at the moment of the call, and they
assert that the material is unchanged afterwards.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_squad.config import SandboxConfig
from agent_squad.models.task import AcceptanceCriterion, CheckKind, Role
from agent_squad.security.capabilities import capabilities_for_role
from agent_squad.security.policy import Decision, PolicyEngine
from agent_squad.tools.base import ToolContext
from agent_squad.tools.exec import RunCommandTool
from agent_squad.tools.fs import DeletePathTool, ReadFileTool, WriteFileTool
from agent_squad.verification.protected import ProtectedMaterial

PROTECTED = "tests/test_hidden.py"


@pytest.fixture()
def worktree(tmp_path: Path) -> Path:
    """A task worktree with a protected file in it."""
    root = tmp_path / "wt"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_hidden.py").write_text(
        "def test_hidden():\n    assert True\n", encoding="utf-8"
    )
    (root / "app.py").write_text("value = 1\n", encoding="utf-8")
    return root


@pytest.fixture()
def context(worktree: Path, tmp_path: Path) -> ToolContext:
    """The context a worker runs with, with the protected material declared."""
    return ToolContext(
        run_id="r",
        task_id="t",
        actor="implementer:t",
        workdir=worktree,
        capabilities=capabilities_for_role(Role.IMPLEMENTER),
        sandbox=SandboxConfig(),
        scope_patterns=(PROTECTED,),
        scope_root=worktree,
        protected_paths=(PROTECTED,),
        protected_root=tmp_path / "protected",
    )


# --------------------------------------------------------------------------
# the material is staged outside the worker's scope
# --------------------------------------------------------------------------


def test_the_protected_material_is_staged_outside_the_worktree(
    worktree: Path, tmp_path: Path
) -> None:
    """The verifier reads the material from outside the task environment."""
    material = ProtectedMaterial(paths=(PROTECTED,)).capture(worktree)
    staged = tmp_path / "protected"
    material.stage(worktree, staged)

    assert material.staged_root == staged
    assert (staged / PROTECTED).exists()
    # The staged copy is outside the worktree, so the file tools -- which
    # resolve every path inside the workdir -- cannot reach it.
    assert staged not in worktree.parents
    assert (worktree / PROTECTED).exists()


def test_the_hidden_evaluator_material_is_not_readable_in_the_task_environment(
    worktree: Path, tmp_path: Path
) -> None:
    """Staging it out of the tree takes it away from the worker entirely."""
    material = ProtectedMaterial(paths=(PROTECTED,)).capture(worktree)
    material.stage(worktree, tmp_path / "protected", remove_from_source=True)

    assert not (worktree / PROTECTED).exists()
    assert (tmp_path / "protected" / PROTECTED).exists()
    # And the verifier still has it.
    intact, _ = material.assert_intact()
    assert intact is True


def test_the_staged_material_is_the_copy_the_verifier_reads(
    worktree: Path, tmp_path: Path
) -> None:
    material = ProtectedMaterial(paths=(PROTECTED,)).capture(worktree)
    material.stage(worktree, tmp_path / "protected")
    verdicts = material.check(worktree)
    assert [v.criterion_id for v in verdicts] == [f"protected:{PROTECTED}"]
    assert verdicts[0].state.value == "established"


def test_a_modified_staged_copy_is_caught_while_the_run_is_in_progress(
    worktree: Path, tmp_path: Path
) -> None:
    """The check that can be made *during* execution."""
    material = ProtectedMaterial(paths=(PROTECTED,)).capture(worktree)
    material.stage(worktree, tmp_path / "protected")

    intact, _ = material.assert_intact()
    assert intact is True

    (tmp_path / "protected" / PROTECTED).write_text("def test_hidden():\n    pass\n")

    intact, reason = material.assert_intact()
    assert intact is False
    assert "modified" in reason
    # And the verdict is void.
    verdicts = material.check(worktree)
    assert any(v.state.value == "not_satisfied" for v in verdicts)


# --------------------------------------------------------------------------
# the file tools refuse the material
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_worker_cannot_rewrite_the_protected_material(
    context: ToolContext, worktree: Path
) -> None:
    """The refusal happens at the call, while the worker is running."""
    tool = WriteFileTool()
    result = await tool.invoke(
        {"path": PROTECTED, "content": "def test_hidden():\n    pass\n"}, context
    )
    assert result.ok is False
    assert result.error_class == "policy"
    assert "protected" in (result.error or "")
    original = (worktree / PROTECTED).read_text(encoding="utf-8")
    assert "assert True" in original


@pytest.mark.integration
async def test_a_worker_cannot_delete_the_protected_material(
    context: ToolContext, worktree: Path
) -> None:
    tool = DeletePathTool()
    result = await tool.invoke({"path": PROTECTED}, context)
    assert result.ok is False
    assert result.error_class == "policy"
    assert (worktree / PROTECTED).exists()


@pytest.mark.integration
async def test_a_worker_cannot_read_the_protected_material(
    context: ToolContext, worktree: Path
) -> None:
    """A worker that can read the oracle can write a check that satisfies it."""
    tool = ReadFileTool()
    result = await tool.invoke({"path": PROTECTED}, context)
    assert result.ok is False
    assert result.error_class == "policy"


@pytest.mark.integration
async def test_a_worker_cannot_reach_the_staged_material_by_path(
    context: ToolContext, tmp_path: Path
) -> None:
    """The staged copy lives outside the worktree, so it is unreachable."""
    tool = WriteFileTool()
    result = await tool.invoke(
        {"path": str(tmp_path / "protected" / PROTECTED), "content": "x"}, context
    )
    assert result.ok is False
    assert result.error_class == "policy"


# --------------------------------------------------------------------------
# the command path refuses the material
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_worker_cannot_rewrite_the_protected_material_through_a_command(
    context: ToolContext, worktree: Path
) -> None:
    """A shell redirection is the cheapest way round a file tool."""
    tool = RunCommandTool()
    result = await tool.invoke(
        {"command": ["sh", "-c", f"echo 'def test_hidden(): pass' > {PROTECTED}"]},
        context,
    )
    assert result.ok is False
    assert result.error_class == "policy"
    assert "assert True" in (worktree / PROTECTED).read_text(encoding="utf-8")


@pytest.mark.integration
async def test_a_worker_cannot_smuggle_a_write_of_the_material_through_python(
    context: ToolContext, worktree: Path
) -> None:
    """The quoted path inside a program is still a path."""
    tool = RunCommandTool()
    result = await tool.invoke(
        {
            "command": [
                "python3",
                "-c",
                f"open('{PROTECTED}','w').write('x')",
            ]
        },
        context,
    )
    assert result.ok is False
    assert result.error_class == "policy"
    assert "assert True" in (worktree / PROTECTED).read_text(encoding="utf-8")


@pytest.mark.integration
async def test_a_worker_cannot_restore_the_material_it_modified(
    context: ToolContext, worktree: Path
) -> None:
    """Modify-then-restore leaves no trace -- so the write must be refused.

    The evidence for this property is the refusal itself: the material was
    never changed, so there was never anything to restore.
    """
    tool = WriteFileTool()
    first = await tool.invoke({"path": PROTECTED, "content": "tampered"}, context)
    assert first.ok is False

    tool2 = RunCommandTool()
    second = await tool2.invoke(
        {"command": ["sh", "-c", f"cp {PROTECTED} {PROTECTED}.bak && rm {PROTECTED}.bak"]},
        context,
    )
    assert second.ok is False
    assert "assert True" in (worktree / PROTECTED).read_text(encoding="utf-8")


@pytest.mark.integration
async def test_a_worker_cannot_write_outside_its_scope_through_a_command(
    context: ToolContext, worktree: Path
) -> None:
    """The command path cannot escape the checks the file tools enforce.

    Two escapes are covered: a path the task's scope *excludes*, and a
    relative escape out of the workspace.  The second is the one that
    matters on a host without a filesystem boundary, because the
    development profile would otherwise let ``../outside`` reach the host.
    """
    tool = RunCommandTool()

    excluded = await tool.invoke(
        {"command": ["sh", "-c", f"echo x > {PROTECTED}"]}, context
    )
    assert excluded.ok is False
    assert excluded.error_class == "policy"

    escape = await tool.invoke(
        {"command": ["sh", "-c", "echo x > ../outside.txt"]}, context
    )
    assert escape.ok is False
    assert escape.error_class == "policy"
    assert not (worktree.parent / "outside.txt").exists()

    ordinary = await tool.invoke(
        {"command": ["sh", "-c", "echo x > app.py"]}, context
    )
    assert ordinary.ok is True


@pytest.mark.integration
async def test_an_ordinary_command_is_still_allowed(context: ToolContext) -> None:
    """The refusal is about the material, not about commands."""
    tool = RunCommandTool()
    result = await tool.invoke(
        {"command": ["python3", "-c", "print('hello')"]}, context
    )
    assert result.ok is True


# --------------------------------------------------------------------------
# the policy engine refuses it too
# --------------------------------------------------------------------------


def test_the_policy_engine_denies_a_write_to_the_protected_material() -> None:
    engine = PolicyEngine(protected_paths=(PROTECTED,))
    decision = engine.authorize(
        run_id="r",
        actor="implementer:t",
        capabilities=capabilities_for_role(Role.IMPLEMENTER),
        tool="write_file",
        arguments={"path": PROTECTED, "content": "x"},
        scope_root="/wt",
    )
    assert decision.decision is Decision.DENY
    assert "protected" in decision.reason


def test_the_policy_engine_denies_a_command_that_touches_the_material() -> None:
    engine = PolicyEngine(protected_paths=(PROTECTED,))
    decision = engine.authorize(
        run_id="r",
        actor="implementer:t",
        capabilities=capabilities_for_role(Role.IMPLEMENTER),
        tool="run_command",
        arguments={"command": ["sh", "-c", f"rm -f {PROTECTED}"]},
        scope_root="/wt",
    )
    assert decision.decision is Decision.DENY
    assert "protected" in decision.reason


def test_the_policy_engine_refuses_the_evaluators_own_configuration() -> None:
    """Configuration manipulation is the quiet way to skip a check."""
    engine = PolicyEngine()
    for path in ("pytest.ini", "conftest.py", "tests/conftest.py"):
        decision = engine.authorize(
            run_id="r",
            actor="implementer:t",
            capabilities=capabilities_for_role(Role.IMPLEMENTER),
            tool="write_file",
            arguments={"path": path, "content": "[pytest]\naddopts = --ignore=hidden\n"},
            scope_root="/wt",
        )
        assert decision.decision is Decision.DENY, path


def test_the_policy_engine_still_allows_an_ordinary_write() -> None:
    engine = PolicyEngine()
    decision = engine.authorize(
        run_id="r",
        actor="implementer:t",
        capabilities=capabilities_for_role(Role.IMPLEMENTER),
        tool="write_file",
        arguments={"path": "app.py", "content": "x"},
        scope_root="/wt",
    )
    assert decision.decision is Decision.ALLOW


# --------------------------------------------------------------------------
# the oracle has to be independent
# --------------------------------------------------------------------------


def test_a_check_that_runs_an_unprotected_file_is_reported() -> None:
    """The worker cannot supply the only oracle for its own change."""
    material = ProtectedMaterial(paths=(PROTECTED,))
    criterion = AcceptanceCriterion(
        id="tests-pass",
        description="the hidden tests pass",
        kind=CheckKind.PYTEST_SELECTOR,
        spec={"selector": "tests/test_worker.py"},
    )
    assert material.missing_oracle_paths((criterion,)) == ("tests/test_worker.py",)

    protected = ProtectedMaterial(paths=(PROTECTED, "tests/test_worker.py"))
    assert protected.missing_oracle_paths((criterion,)) == ()
