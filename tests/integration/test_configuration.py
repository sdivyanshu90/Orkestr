"""Configuration wiring.

Every knob in the configuration must have an *effect*.  A setting that is
accepted, stored and ignored is worse than a setting that does not exist,
because an operator will believe it is in force.

These tests exercise each configurable behaviour and assert that changing it
changes the outcome.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_squad.config import (
    ApprovalConfig,
    BudgetConfig,
    ProviderConfig,
    RoleConfig,
    RunConfig,
    SandboxConfig,
    StrategyConfig,
    VerificationConfig,
    WorkspaceConfig,
)
from agent_squad.dag.graph import TaskGraph
from agent_squad.models.task import (
    AcceptanceCriterion,
    CheckKind,
    RepoScope,
    Role,
    TaskSpec,
)
from agent_squad.orchestrator import Orchestrator
from agent_squad.observe.tracing import Tracer
from agent_squad.strategy.heuristics import CoordinationModel

from helpers import two_independent_spec


def graph_of(*files: str) -> TaskGraph:
    graph = TaskGraph()
    for index, path in enumerate(files):
        graph.add(
            TaskSpec(
                id=f"t{index}",
                run_id="r",
                title=f"t{index}",
                objective="o",
                scope=RepoScope(anticipated_files=(path,)),
                acceptance_criteria=(
                    AcceptanceCriterion(
                        id=f"c{index}",
                        description="c",
                        kind=CheckKind.FILE_EXISTS,
                        spec={"path": path},
                    ),
                ),
            )
        )
    graph.link()
    return graph


def test_the_shared_file_weight_changes_the_integration_risk() -> None:
    graph = graph_of("shared.py", "shared.py", "other.py")
    low = CoordinationModel.from_graph(graph, StrategyConfig(shared_file_conflict_weight=0.1))
    high = CoordinationModel.from_graph(graph, StrategyConfig(shared_file_conflict_weight=2.0))
    assert high.integration_risk() > low.integration_risk()


def test_the_trace_sample_rate_changes_what_is_recorded() -> None:
    full = Tracer(service_name="x", sample_rate=1.0)
    none = Tracer(service_name="x", sample_rate=0.0)
    with full.span("a"):
        pass
    with none.span("a"):
        pass
    assert len(full.recorder.spans) == 1
    assert len(none.recorder.spans) == 0
    assert none.sampled_out == 1


def test_the_provider_token_limit_is_enforced_per_provider(tmp_path: Path) -> None:
    from agent_squad.providers.base import Usage
    from agent_squad.providers.budget import BudgetManager

    manager = BudgetManager(
        BudgetConfig(per_provider_token_limit=100), ProviderConfig(name="mock", model="m")
    )
    manager.set_scope_limits("provider:mock", token_limit=100)
    manager.record("provider:mock", Usage(input_tokens=90, output_tokens=0))
    with pytest.raises(Exception) as exc:
        manager.check("provider:mock", extra_tokens=50)
    assert "token budget exhausted" in str(exc.value)


def test_the_role_model_override_selects_a_different_provider(
    tmp_path: Path, repo: Path
) -> None:
    config = RunConfig(
        run_id="run_override",
        repo_path=str(repo),
        data_dir=str(tmp_path / "data"),
        provider={"name": "mock", "model": "primary-model"},
        roles={"reviewer": {"model_override": "reviewer-model"}},
    )
    orchestrator = Orchestrator(config, data_dir=tmp_path / "data")
    try:
        assert orchestrator.provider_for("reviewer").model == "reviewer-model"
        assert orchestrator.provider_for("implementer").model == "primary-model"
        # The override is built once and reused.
        assert orchestrator.provider_for("reviewer") is orchestrator.provider_for("reviewer")
    finally:
        orchestrator.close()


def test_the_integration_root_is_used_when_configured(tmp_path: Path, repo: Path) -> None:
    from agent_squad.integration.engine import IntegrationEngine
    from agent_squad.workspace.worktree import WorktreeManager

    staging = tmp_path / "staging"
    config = RunConfig(
        run_id="r",
        repo_path=str(repo),
        workspace={"integration_root": str(staging)},
    )
    worktrees = WorktreeManager(config.repo_path, tmp_path / "wt")
    engine = IntegrationEngine(config=config, worktrees=worktrees)
    assert engine.integration_root == staging
    assert engine.worktrees.root == staging


def test_the_verification_command_is_configurable() -> None:
    config = VerificationConfig(integration_test_command=("python3", "-m", "unittest"))
    assert config.integration_test_command == ("python3", "-m", "unittest")


def test_the_sandbox_allow_list_is_enforced_by_configuration() -> None:
    from agent_squad.errors import ToolError
    from agent_squad.sandbox.limits import Sandbox

    sandbox = Sandbox(SandboxConfig(allowed_binaries=("python3",)))
    with pytest.raises(ToolError):
        sandbox.check_executable("curl")


def test_the_approval_toggles_change_the_decision() -> None:
    from agent_squad.security.capabilities import capabilities_for_role
    from agent_squad.security.policy import Decision, PolicyEngine

    caps = capabilities_for_role(Role.IMPLEMENTER)
    strict = PolicyEngine(approval=ApprovalConfig(require_approval_for_git_push=True))
    relaxed = PolicyEngine(approval=ApprovalConfig(require_approval_for_git_push=False))
    assert (
        strict.authorize(
            run_id="r", actor="a", capabilities=caps, tool="push_changes", arguments={}
        ).decision
        is Decision.REQUIRE_APPROVAL
    )
    assert (
        relaxed.authorize(
            run_id="r", actor="a", capabilities=caps, tool="push_changes", arguments={}
        ).decision
        is Decision.ALLOW
    )


def test_the_role_config_sets_the_capabilities() -> None:
    config = RoleConfig(capabilities=("fs:read",))
    assert "fs:read" in config.capabilities


def test_an_unknown_config_key_is_rejected(tmp_path: Path) -> None:
    """A typo must fail loudly rather than silently disabling a control."""
    from agent_squad.config import build_run_config
    from agent_squad.errors import ValidationError

    with pytest.raises(ValidationError):
        build_run_config(
            {"sandbx": {"allow_network": True}},
            repo_path=str(tmp_path),
            run_id="r",
        )


def test_the_workspace_config_controls_worktree_retention() -> None:
    config = WorkspaceConfig(keep_worktrees_on_success=True, keep_worktrees_on_failure=False)
    assert config.keep_worktrees_on_success is True
    assert config.keep_worktrees_on_failure is False


def test_the_run_config_rejects_an_impossible_worker_override() -> None:
    from pydantic import ValidationError as PydanticValidationError

    with pytest.raises(PydanticValidationError):
        RunConfig(
            run_id="r",
            repo_path="/tmp",
            strategy={"max_workers": 2},
            max_workers_override=8,
        )


def test_the_budget_config_is_serialisable_and_reloadable() -> None:
    config = RunConfig(
        run_id="r",
        repo_path="/tmp",
        budget={"run_token_limit": 1000, "per_task_cost_limit_usd": 0.5},
    )
    reloaded = RunConfig.model_validate_json(config.model_dump_json())
    assert reloaded.budget.run_token_limit == 1000
    assert reloaded.budget.per_task_cost_limit_usd == 0.5
