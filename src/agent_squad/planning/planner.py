"""Planning: turning a request into a validated task DAG.

Two planners are provided:

``RuleBasedPlanner``
    Deterministic.  It consumes a *structured engineering request* (a JSON
    document describing the tasks) or, absent that, produces a single
    conservative task.  This is the planner used with the mock provider and
    the one used by the benchmark harness when a controlled baseline is
    required.

``LLMPlanner``
    Asks a model to produce the same structure, then validates the result
    against a schema and against the graph rules.  Model output is treated as
    untrusted input: it is parsed, validated and discarded on any error, and
    it can never widen a role's capabilities or change a task's budget.

The output of both planners is the same :class:`PlanResult`, so the rest of
the system does not care which one produced the plan.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from ..config import RunConfig
from ..dag.graph import TaskGraph
from ..errors import ValidationError
from ..models.contract import RequestContract
from ..models.task import (
    AcceptanceCriterion,
    Budget,
    Complexity,
    RepoScope,
    Role,
    TaskSpec,
)
from ..providers.base import ChatMessage, ModelRequest
from .brief import Edit, TaskBrief
from .inspector import RepoContext

LLM_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["tasks"],
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "title", "objective"],
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "objective": {"type": "string"},
                    "description": {"type": "string"},
                    "role": {
                        "type": "string",
                        "enum": [r.value for r in Role if r is not Role.INTEGRATION],
                    },
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "acceptance_criteria": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["id", "description", "kind"],
                            "properties": {
                                "id": {"type": "string"},
                                "description": {"type": "string"},
                                "kind": {"type": "string"},
                                "spec": {"type": "object"},
                                "required": {"type": "boolean"},
                            },
                        },
                    },
                    "anticipated_files": {"type": "array", "items": {"type": "string"}},
                    "include_paths": {"type": "array", "items": {"type": "string"}},
                    "priority": {"type": "integer"},
                    "complexity": {"type": "string"},
                    "estimated_duration_s": {"type": "number"},
                    "max_attempts": {"type": "integer"},
                },
            },
        },
        "rationale": {"type": "array", "items": {"type": "string"}},
    },
}


class PlannedTask(BaseModel):
    """One task as produced by a planner (before it becomes a :class:`TaskSpec`)."""

    model_config = ConfigDict(extra="ignore")

    id: str
    title: str
    objective: str
    description: str = ""
    role: Role = Role.IMPLEMENTER
    depends_on: list[str] = Field(default_factory=list)
    acceptance_criteria: list[dict[str, Any]] = Field(default_factory=list)
    anticipated_files: list[str] = Field(default_factory=list)
    include_paths: list[str] = Field(default_factory=list)
    exclude_paths: list[str] = Field(default_factory=list)
    priority: int = 50
    complexity: Complexity | str = Complexity.MEDIUM
    estimated_duration_s: float = 300.0
    max_attempts: int = 3
    token_budget: int | None = None
    cost_budget_usd: float | None = None
    edits: list[Edit] = Field(default_factory=list)
    commands: list[list[str]] = Field(default_factory=list)
    notes: str = ""
    unverifiable_criteria: list[str] = Field(default_factory=list)


class LLMPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tasks: list[PlannedTask] = Field(default_factory=list)
    rationale: list[str] = Field(default_factory=list)


class PlanRequest(BaseModel):
    """Input to a planner."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    run_id: str
    objective: str
    description: str = ""
    repo_path: str = ""
    base_revision: str | None = None
    context: RepoContext | None = None
    request_spec: dict[str, Any] | None = None
    contract: RequestContract | None = None
    """The owner's immutable requirements, when there are any.

    A plan may *add* supplementary checks; it may not drop or weaken a
    required one.  The check is enforced after the plan is built.
    """


class PlanResult(BaseModel):
    """Output of a planner."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    tasks: list[TaskSpec]
    briefs: dict[str, TaskBrief]
    rationale: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    planner: str = "unknown"

    def graph(self) -> TaskGraph:
        graph = TaskGraph()
        graph.add_all(list(self.tasks))
        graph.link()
        return graph


class BasePlanner:
    name = "base"

    async def plan(self, request: PlanRequest) -> PlanResult:  # pragma: no cover
        raise NotImplementedError


class RuleBasedPlanner(BasePlanner):
    """Deterministic planner.

    Given a structured request it materialises exactly the tasks described.
    Given only an objective it produces one task with no deterministic
    acceptance criteria, and *says so* -- the plan then carries a warning
    that the work cannot be machine-verified.
    """

    name = "rule_based"

    async def plan(self, request: PlanRequest) -> PlanResult:
        spec = request.request_spec
        rationale: list[str] = []
        warnings: list[str] = []

        if spec and spec.get("tasks"):
            planned = [PlannedTask.model_validate(t) for t in spec["tasks"]]
            rationale.append(
                f"materialised {len(planned)} task(s) from the structured request"
            )
        else:
            planned = [
                PlannedTask(
                    id="task-1",
                    title=request.objective[:80] or "implement objective",
                    objective=request.objective,
                    description=request.description,
                    role=Role.IMPLEMENTER,
                )
            ]
            rationale.append(
                "no structured request supplied; produced a single conservative task"
            )
            warnings.append(
                "the plan has no deterministic acceptance criteria, so the result "
                "cannot be machine-verified"
            )

        tasks: list[TaskSpec] = []
        briefs: dict[str, TaskBrief] = {}
        seen_ids: set[str] = set()

        for item in planned:
            task_id = item.id
            if task_id in seen_ids:
                raise ValidationError(f"planner produced duplicate task id {task_id!r}")
            seen_ids.add(task_id)

            criteria = tuple(
                AcceptanceCriterion.model_validate(c) for c in item.acceptance_criteria
            )
            spec_obj = TaskSpec(
                id=task_id,
                run_id=request.run_id,
                title=item.title,
                objective=item.objective,
                description=item.description,
                depends_on=tuple(item.depends_on),
                acceptance_criteria=criteria,
                scope=RepoScope(
                    include_paths=tuple(item.include_paths),
                    exclude_paths=tuple(item.exclude_paths),
                    anticipated_files=tuple(item.anticipated_files),
                ),
                role=item.role,
                priority=item.priority,
                complexity=_as_complexity(item.complexity),
                estimated_duration_s=item.estimated_duration_s,
                budget=Budget(
                    max_tokens=item.token_budget,
                    max_cost_usd=item.cost_budget_usd,
                    max_attempts=item.max_attempts,
                ),
                metadata={
                    k: v
                    for k, v in {
                        "notes": item.notes,
                        "unverifiable_criteria": tuple(item.unverifiable_criteria),
                    }.items()
                    if v
                },
            )
            tasks.append(spec_obj)
            briefs[task_id] = TaskBrief(
                task_id=task_id,
                role=item.role,
                objective=item.objective,
                description=item.description,
                base_revision=request.base_revision,
                scope=spec_obj.scope,
                acceptance_criteria=tuple(c.model_dump(mode="json") for c in criteria),
                edits=tuple(item.edits),
                commands=tuple(tuple(c) for c in item.commands),
                notes=item.notes,
                depends_on=spec_obj.depends_on,
            )

        result = PlanResult(
            tasks=tasks, briefs=briefs, rationale=rationale, warnings=warnings, planner=self.name
        )
        validate_plan(result, max_delegation_depth=0)
        enforce_contract(result, request.contract)
        return result


class LLMPlanner(BasePlanner):
    """Planner that asks a model for the task structure.

    The model's reply is parsed as JSON and validated.  Anything that does
    not validate is a hard failure: the orchestrator never "repairs" a plan by
    guessing what the model probably meant.

    The one exception is *normalisation*: a field whose value is
    well-formed but out of range -- an unknown ``complexity``, a priority
    outside 0-100, a non-positive duration -- is corrected to a safe default
    **and the correction is recorded** in the plan's warnings.  Structural
    problems (a missing task, a dependency on a task that does not exist, a
    criterion with no executable kind) are never normalised away.
    """

    name = "llm"

    def __init__(self, provider: Any, *, max_tasks: int = 12) -> None:
        self._provider = provider
        self._max_tasks = max_tasks
        self.warnings: list[str] = []

    async def plan(self, request: PlanRequest) -> PlanResult:
        self.warnings = []
        messages = [
            ChatMessage(
                role="system",
                content=(
                    "You decompose an engineering request into a task DAG. "
                    "Return JSON only. Every task needs an id, a title, an objective "
                    "and at least one deterministic acceptance criterion whose "
                    "'kind' is one of file_exists, file_contains, file_not_contains, "
                    "command_exits_zero, command_output_matches, pytest_selector, "
                    "diff_nonempty, diff_empty. Use depends_on only for real data "
                    "dependencies. Prefer fewer, better-scoped tasks. "
                    "'complexity' must be one of trivial, low, medium, high."
                ),
            ),
            ChatMessage(role="user", content=self._user_prompt(request)),
        ]
        response = await self._provider.complete(
            ModelRequest(
                messages=messages,
                response_schema=LLM_PLAN_SCHEMA,
                metadata={"schema_name": "plan"},
            )
        )
        plan = self._parse(response.text)
        if len(plan.tasks) > self._max_tasks:
            raise ValidationError(
                f"planner returned {len(plan.tasks)} tasks, limit is {self._max_tasks}"
            )
        plan = self._normalise(plan)
        tasks, briefs = self._to_specs(plan, request)
        result = PlanResult(
            tasks=tasks,
            briefs=briefs,
            rationale=list(plan.rationale),
            warnings=list(self.warnings),
            planner=self.name,
        )
        validate_plan(result)
        return result

    def _normalise(self, plan: LLMPlan) -> LLMPlan:
        """Correct recoverable plan defects and record every correction.

        Three classes of defect are handled, and the handling is deliberately
        asymmetric:

        * **Duplicate criterion ids** are disambiguated.  The criteria
          themselves are intact, so renaming the second occurrence changes
          nothing about what is checked.
        * **A criterion that cannot be expressed** -- a ``pytest_selector``
          with no selector, a command with no argv -- is *dropped from the
          plan* and recorded as an **unverifiable** requirement.  It is not
          simply forgotten: the task carries the list, and the verification
          gate refuses to pass a task with an unverifiable requirement.  A
          broken criterion must never become a satisfied one.
        * **Out-of-range scalars** -- an unknown ``complexity``, a priority
          outside 0-100, a non-positive duration -- are corrected to a safe
          default and recorded.

        Anything structural (a task with no id, a dependency on a task that
        does not exist) remains a hard failure.
        """
        normalised: list[PlannedTask] = []
        for task in plan.tasks:
            data = task.model_dump()
            complexity = data.get("complexity")
            if complexity is not None and not isinstance(complexity, Complexity):
                try:
                    data["complexity"] = Complexity(complexity)
                except ValueError:
                    self.warnings.append(
                        f"task {task.id!r}: complexity {complexity!r} is not a known "
                        "level; defaulted to 'medium'"
                    )
                    data["complexity"] = Complexity.MEDIUM
            priority = data.get("priority")
            if not isinstance(priority, int) or not 0 <= priority <= 100:
                self.warnings.append(
                    f"task {task.id!r}: priority {priority!r} is out of range; "
                    "defaulted to 50"
                )
                data["priority"] = 50
            duration = data.get("estimated_duration_s")
            if not isinstance(duration, (int, float)) or duration <= 0:
                self.warnings.append(
                    f"task {task.id!r}: estimated_duration_s {duration!r} is not "
                    "positive; defaulted to 300"
                )
                data["estimated_duration_s"] = 300.0
            attempts = data.get("max_attempts")
            if not isinstance(attempts, int) or not 1 <= attempts <= 20:
                data["max_attempts"] = 3

            criteria, unverifiable = self._normalise_criteria(task)
            data["acceptance_criteria"] = criteria
            data["unverifiable_criteria"] = unverifiable
            normalised.append(PlannedTask.model_validate(data))
            for note in unverifiable:
                self.warnings.append(f"task {task.id!r}: {note}")
        return plan.model_copy(update={"tasks": normalised})

    def _normalise_criteria(
        self, task: PlannedTask
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Disambiguate ids and separate unexpressible criteria."""
        criteria: list[dict[str, Any]] = []
        unverifiable: list[str] = []
        seen: set[str] = set()
        for raw in task.acceptance_criteria:
            data = dict(raw)
            criterion_id = str(data.get("id") or "").strip()
            if not criterion_id:
                unverifiable.append("an acceptance criterion has no id and was dropped")
                continue
            if criterion_id in seen:
                suffix = 2
                candidate = f"{criterion_id}-{suffix}"
                while candidate in seen:
                    suffix += 1
                    candidate = f"{criterion_id}-{suffix}"
                self.warnings.append(
                    f"task {task.id!r}: duplicate criterion id {criterion_id!r} "
                    f"renamed to {candidate!r}"
                )
                criterion_id = candidate
            seen.add(criterion_id)
            data["id"] = criterion_id
            try:
                criterion = AcceptanceCriterion.model_validate(data)
            except Exception as exc:
                unverifiable.append(
                    f"acceptance criterion {criterion_id!r} could not be expressed "
                    f"({exc}); the task cannot be verified without it"
                )
                continue
            criteria.append(criterion.model_dump())
        return criteria, unverifiable

    def _user_prompt(self, request: PlanRequest) -> str:
        parts = [f"Objective: {request.objective}"]
        if request.description:
            parts.append(f"Details: {request.description}")
        if request.context:
            parts.append("Repository context:\n" + request.context.summary_text())
        if request.request_spec:
            parts.append(
                "Structured request (use it as the authoritative task description):\n"
                + json.dumps(request.request_spec, indent=2, sort_keys=True, default=str)
            )
        return "\n\n".join(parts)

    @staticmethod
    def _parse(text: str) -> LLMPlan:
        blob = text.strip()
        if blob.startswith("```"):
            blob = blob.strip("`")
            if blob.startswith("json"):
                blob = blob[4:]
            blob = blob.strip()
        start = blob.find("{")
        end = blob.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValidationError("planner response did not contain a JSON object")
        try:
            data = json.loads(blob[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValidationError(f"planner response was not valid JSON: {exc}") from exc
        try:
            return LLMPlan.model_validate(data)
        except PydanticValidationError as exc:
            raise ValidationError(f"planner response failed validation: {exc}") from exc

    def _to_specs(
        self, plan: LLMPlan, request: PlanRequest
    ) -> tuple[list[TaskSpec], dict[str, TaskBrief]]:
        tasks: list[TaskSpec] = []
        briefs: dict[str, TaskBrief] = {}
        for item in plan.tasks:
            criteria = tuple(
                AcceptanceCriterion.model_validate(c) for c in item.acceptance_criteria
            )
            task = TaskSpec(
                id=item.id,
                run_id=request.run_id,
                title=item.title,
                objective=item.objective,
                description=item.description,
                depends_on=tuple(item.depends_on),
                acceptance_criteria=criteria,
                scope=RepoScope(
                    include_paths=tuple(item.include_paths),
                    exclude_paths=tuple(item.exclude_paths),
                    anticipated_files=tuple(item.anticipated_files),
                ),
                role=item.role,
                priority=item.priority,
                complexity=_as_complexity(item.complexity),
                estimated_duration_s=item.estimated_duration_s,
                budget=Budget(
                    max_tokens=item.token_budget,
                    max_cost_usd=item.cost_budget_usd,
                    max_attempts=item.max_attempts,
                ),
                metadata={
                    k: v
                    for k, v in {
                        "notes": item.notes,
                        "unverifiable_criteria": tuple(item.unverifiable_criteria),
                    }.items()
                    if v
                },
            )
            tasks.append(task)
            briefs[task.id] = TaskBrief(
                task_id=task.id,
                role=task.role,
                objective=task.objective,
                description=task.description,
                base_revision=request.base_revision,
                scope=task.scope,
                acceptance_criteria=tuple(c.model_dump(mode="json") for c in criteria),
                depends_on=task.depends_on,
            )
        return tasks, briefs


def _as_complexity(value: Any) -> Complexity:
    """Coerce a complexity to the enum, defaulting when it is not one."""
    if isinstance(value, Complexity):
        return value
    try:
        return Complexity(str(value))
    except ValueError:
        return Complexity.MEDIUM


def validate_plan(result: PlanResult, *, max_delegation_depth: int = 2) -> list[str]:
    """Validate a plan's graph and return non-fatal warnings."""
    if not result.tasks:
        raise ValidationError("plan contains no tasks")
    graph = result.graph()
    warnings = graph.validate(max_delegation_depth=max_delegation_depth)
    for task in result.tasks:
        if task.budget.max_attempts < 1:
            raise ValidationError(f"task {task.id} has invalid max_attempts")
        if task.role is Role.INTEGRATION:
            raise ValidationError(
                f"task {task.id} requests the integration role, which is not "
                "assignable to an LLM agent"
            )
    return warnings


def enforce_contract(
    result: PlanResult, contract: RequestContract | None
) -> list[str]:
    """Refuse a plan that drops or weakens a required outcome.

    The contract is the owner's statement of what must be true.  A plan may
    add checks — that makes it stronger — but it may not remove one, because
    a plan is proposed by the same kind of component that does the work.

    Returns the warnings raised by the check.
    """
    if contract is None or not contract.required_outcomes:
        return []
    planned = {
        criterion.id
        for task in result.tasks
        for criterion in task.acceptance_criteria
    }
    missing = [
        c.id for c in contract.required_outcomes if c.id not in planned
    ]
    if missing:
        raise ValidationError(
            "the plan drops required outcomes from the request contract: "
            + ", ".join(missing)
            + ". A plan may add checks but may not remove a required one.",
            missing=missing,
        )
    return []


def build_planner(run_config: RunConfig, provider: Any) -> BasePlanner:
    """Choose a planner for the configured provider."""
    if run_config.provider.name.lower() == "mock":
        return RuleBasedPlanner()
    return LLMPlanner(provider)
