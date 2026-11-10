"""Plan normalisation tests.

A model that plans a task set produces defects.  The planner's job is to
distinguish the defects that can be corrected without changing what is
checked from the defects that must fail the run.

The rule the tests pin down:

* a **duplicate criterion id** is corrected -- the criteria themselves are
  intact, so renaming changes nothing about what is verified;
* an **unexpressible criterion** is dropped from the plan *and* recorded as
  an unverifiable requirement, so the task cannot be silently verified
  without it;
* a **structural defect** still fails the plan outright.
"""

from __future__ import annotations

import json

import pytest

from agent_squad.errors import ValidationError
from agent_squad.planning.planner import LLMPlanner
from agent_squad.providers.base import ModelRequest, ModelResponse


class ScriptedPlannerProvider:
    """Returns a fixed plan."""

    def __init__(self, plan: dict) -> None:
        self.plan = plan
        self.prompts: list[str] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.prompts.extend(m.content for m in request.messages)
        return ModelResponse(
            text=json.dumps(self.plan), provider="scripted", model="s"
        )


def make_planner(plan: dict) -> LLMPlanner:
    return LLMPlanner(ScriptedPlannerProvider(plan))


def base_task(**overrides) -> dict:
    task = {
        "id": "t1",
        "title": "Do the thing",
        "objective": "do it",
        "acceptance_criteria": [
            {
                "id": "c1",
                "description": "a file exists",
                "kind": "file_exists",
                "spec": {"path": "a.py"},
            }
        ],
    }
    task.update(overrides)
    return task


@pytest.mark.integration
async def test_duplicate_criterion_ids_are_disambiguated() -> None:
    plan = {
        "tasks": [
            base_task(
                acceptance_criteria=[
                    {
                        "id": "same",
                        "description": "first",
                        "kind": "file_exists",
                        "spec": {"path": "a.py"},
                    },
                    {
                        "id": "same",
                        "description": "second",
                        "kind": "file_exists",
                        "spec": {"path": "b.py"},
                    },
                ]
            )
        ],
        "rationale": [],
    }
    planner = make_planner(plan)
    from agent_squad.planning.planner import PlanRequest

    result = await planner.plan(PlanRequest(run_id="r", objective="o"))
    task = result.tasks[0]
    ids = [c.id for c in task.acceptance_criteria]
    assert len(ids) == 2
    assert len(set(ids)) == 2
    assert ids[0] == "same"
    assert ids[1] != "same"
    # Both criteria survive: nothing was dropped.
    assert len(task.acceptance_criteria) == 2
    assert any("duplicate criterion id" in w for w in result.warnings)


@pytest.mark.integration
async def test_an_unexpressible_criterion_is_recorded_as_unverifiable() -> None:
    plan = {
        "tasks": [
            base_task(
                acceptance_criteria=[
                    {
                        "id": "ok",
                        "description": "fine",
                        "kind": "file_exists",
                        "spec": {"path": "a.py"},
                    },
                    {
                        "id": "broken",
                        "description": "no selector",
                        "kind": "pytest_selector",
                        "spec": {},
                    },
                ]
            )
        ],
        "rationale": [],
    }
    planner = make_planner(plan)
    from agent_squad.planning.planner import PlanRequest

    result = await planner.plan(PlanRequest(run_id="r", objective="o"))
    task = result.tasks[0]
    # The broken criterion is not in the plan...
    assert [c.id for c in task.acceptance_criteria] == ["ok"]
    # ...but it is recorded, so the task cannot be verified without it.
    unverifiable = task.metadata.get("unverifiable_criteria") or ()
    assert len(unverifiable) == 1
    assert "broken" in unverifiable[0]
    assert any("could not be expressed" in w for w in result.warnings)


@pytest.mark.integration
async def test_a_task_with_an_unverifiable_requirement_cannot_pass_the_gate(
    tmp_path,
) -> None:
    """The rigour must not be lost: a broken requirement is not a met one."""
    from agent_squad.config import RunConfig
    from agent_squad.models.task import TaskSpec
    from agent_squad.verification.gate import VerificationGate

    spec = TaskSpec(
        id="t1",
        run_id="r",
        title="t",
        objective="o",
        acceptance_criteria=(),
        metadata={"unverifiable_criteria": ("criterion 'broken' could not be expressed",)},
    )
    gate = VerificationGate(RunConfig(run_id="r", repo_path=str(tmp_path)))
    report = await gate.verify_task(spec, workspace=tmp_path)
    assert report.passed is False
    assert "could not be expressed" in (report.error or "")
    assert report.unverified


@pytest.mark.integration
async def test_an_unknown_complexity_is_normalised_and_recorded() -> None:
    plan = {
        "tasks": [base_task(complexity="path")],
        "rationale": [],
    }
    planner = make_planner(plan)
    from agent_squad.planning.planner import PlanRequest

    result = await planner.plan(PlanRequest(run_id="r", objective="o"))
    assert result.tasks[0].complexity.value == "medium"
    assert any("complexity" in w for w in result.warnings)


@pytest.mark.integration
async def test_an_out_of_range_priority_is_normalised() -> None:
    plan = {"tasks": [base_task(priority=5000)], "rationale": []}
    planner = make_planner(plan)
    from agent_squad.planning.planner import PlanRequest

    result = await planner.plan(PlanRequest(run_id="r", objective="o"))
    assert result.tasks[0].priority == 50
    assert any("priority" in w for w in result.warnings)


@pytest.mark.integration
async def test_a_structural_defect_still_fails_the_plan() -> None:
    """Normalisation must not paper over a missing task."""
    from agent_squad.planning.planner import PlanRequest

    planner = make_planner({"tasks": [], "rationale": []})
    with pytest.raises(ValidationError):
        await planner.plan(PlanRequest(run_id="r", objective="o"))


@pytest.mark.integration
async def test_a_dependency_on_an_unknown_task_still_fails() -> None:
    from agent_squad.errors import DependencyError
    from agent_squad.planning.planner import PlanRequest

    plan = {"tasks": [base_task(depends_on=["nope"])], "rationale": []}
    planner = make_planner(plan)
    with pytest.raises(DependencyError):
        await planner.plan(PlanRequest(run_id="r", objective="o"))
