"""Building the request contract from the owner's declaration.

The contract is the boundary between *what was asked for* and *what the
planner decided to do*.  It is built from the structured request the owner
supplied, never from the model's output.

When the owner supplies no structured request, the contract has **no
required outcomes**.  That is not a gap in the implementation; it is the
honest position: without a stated requirement there is nothing independent
against which the result can be judged, so the run is reported as
*inconclusive* rather than *accepted*.
"""

from __future__ import annotations

from typing import Any

from ..errors import ValidationError
from ..models.contract import RequestContract
from ..models.task import AcceptanceCriterion, RepoScope


def build_contract(
    objective: str,
    request_spec: dict[str, Any] | None,
    *,
    description: str = "",
) -> RequestContract:
    """Derive the contract from the owner's structured request.

    The required outcomes are the union of every task's acceptance criteria
    in the owner's specification.  A criterion that cannot be expressed is a
    hard error: the owner asked for something that cannot be checked, and
    silently dropping it would weaken the request.
    """
    if not request_spec or not request_spec.get("tasks"):
        return RequestContract(
            objective=objective,
            notes=(
                "no structured request was supplied, so the contract states no "
                "required outcome; the run can be reported but not accepted"
            ),
        )

    outcomes: list[AcceptanceCriterion] = []
    seen: set[str] = set()
    scope = RepoScope()
    for task in request_spec["tasks"]:
        for raw in task.get("acceptance_criteria") or []:
            try:
                criterion = AcceptanceCriterion.model_validate(raw)
            except Exception as exc:
                raise ValidationError(
                    f"the request's acceptance criterion for task "
                    f"{task.get('id')!r} is not expressible: {exc}"
                ) from exc
            if criterion.id in seen:
                # The same criterion in two tasks is one requirement.
                continue
            seen.add(criterion.id)
            outcomes.append(criterion)
        include = tuple(task.get("include_paths") or ())
        exclude = tuple(task.get("exclude_paths") or ())
        if include or exclude:
            scope = RepoScope(
                include_paths=tuple(sorted(set(scope.include_paths) | set(include))),
                exclude_paths=tuple(sorted(set(scope.exclude_paths) | set(exclude))),
            )

    return RequestContract(
        objective=objective,
        required_outcomes=tuple(outcomes),
        scope=scope,
        notes=description,
    )
