"""The orchestrator: the composition root for a single run.

This module owns the wiring that the rest of the system deliberately does
not do: creating the run's storage, inspecting the repository, planning,
choosing a strategy, scheduling the workers, integrating the resulting
patches and reporting the outcome.

Everything the orchestrator does is recorded in the event log, and every
component it constructs receives the run's configuration rather than reading
globals.  Two runs with the same configuration and the same inputs produce
the same *control-flow decisions*; they do not produce the same model
outputs, and the run records the difference.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agents.approval import ApprovalBroker
from .agents.messaging import MessageBus, SupervisorChannel
from .agents.runner import WorkerRunner
from .artifacts.store import ArtifactStore
from .config import RunConfig
from .dag.graph import TaskGraph
from .errors import RunOwnershipError, ValidationError
from .integration.engine import IntegrationEngine
from .integration.patch import IntegrationResult, PatchArtifact
from .models.artifact import ArtifactKind
from .models.contract import (
    ContractVerdict,
    OutcomeState,
    RequestContract,
    VerificationOutcome,
)
from .models.run import RunStatus, RunSummary, StrategyDecision
from .models.task import TaskStatus
from .observe.events import StructuredLogger
from .observe.tracing import Tracer
from .planning.contract import build_contract
from .planning.inspector import RepositoryInspector
from .planning.planner import PlanRequest, PlanResult, build_planner
from .planning.replan import (
    RevisionContext,
    check_revision,
    merge_revisions,
)
from .providers.base import ModelProvider
from .providers.budget import BudgetManager
from .providers.registry import ProviderRouter, build_providers
from .runtime.deadline import RunDeadline
from .scheduler.engine import Scheduler, TaskOutcome
from .security.policy import PolicyEngine
from .security.profiles import IsolationGuard, get_profile
from .store.db import Database
from .store.events import EventLog, EventType
from .store.state import StateStore
from .strategy.selector import StrategySelector
from .verification.contract_gate import ContractGate
from .verification.gate import VerificationGate
from .verification.protected import ProtectedMaterial
from .workspace.worktree import WorktreeManager

SUPERVISOR = "supervisor"


@dataclass
class RunPaths:
    """Where a run keeps its files."""

    root: Path

    @classmethod
    def for_run(cls, data_dir: str | Path, run_id: str) -> RunPaths:
        return cls(root=Path(data_dir) / "runs" / run_id)

    @property
    def database(self) -> Path:
        return self.root / "state.db"

    @property
    def artifacts(self) -> Path:
        return self.root / "artifacts"

    @property
    def worktrees(self) -> Path:
        return self.root / "worktrees"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    def ensure(self) -> None:
        for path in (self.root, self.artifacts, self.worktrees, self.logs):
            path.mkdir(parents=True, exist_ok=True)


@dataclass
class RunOutcome:
    """Everything the caller needs to know about a completed run."""

    run_id: str
    status: RunStatus
    summary: RunSummary
    decision: StrategyDecision | None = None
    plan: PlanResult | None = None
    outcomes: dict[str, TaskOutcome] = field(default_factory=dict)
    integration: IntegrationResult | None = None
    patch_bundle: str | None = None
    started_at: _dt.datetime | None = None
    finished_at: _dt.datetime | None = None
    warnings: list[str] = field(default_factory=list)
    contract: ContractVerdict | None = None
    """The acceptance judgement against the request contract, if one was made."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status.value,
            "summary": self.summary.model_dump(mode="json"),
            "decision": self.decision.model_dump(mode="json") if self.decision else None,
            "tasks": {k: v.to_dict() for k, v in self.outcomes.items()},
            "integration": self.integration.to_dict() if self.integration else None,
            "contract": self.contract.to_dict() if self.contract else None,
            "warnings": list(self.warnings),
        }


class Orchestrator:
    """Runs one orchestration session end to end."""

    def __init__(
        self,
        config: RunConfig,
        *,
        data_dir: str | Path | None = None,
        provider: ModelProvider | None = None,
        record_exchanges: bool = True,
        auto_approve: bool = False,
    ) -> None:
        self.config = config
        base = Path(data_dir or config.data_dir or ".agent-squad").resolve()
        self.paths = RunPaths.for_run(base, config.run_id)
        self.paths.ensure()
        # Persist the configuration so that the run can be replayed later
        # without the operator having to reconstruct it.
        (self.paths.root / "run-config.json").write_text(
            config.model_dump_json(indent=2), encoding="utf-8"
        )

        self.db = Database(self.paths.database)
        self.log = EventLog(self.db)
        self.state = StateStore(self.db, self.log)
        self.artifacts = ArtifactStore(self.paths.artifacts)
        self.worktrees = WorktreeManager(config.repo_path, self.paths.worktrees)
        self.policy = PolicyEngine(
            sandbox=config.sandbox, approval=config.approval
        )
        self.gate = VerificationGate(config, self.state, self.log)
        self.bus = MessageBus(
            state=self.state,
            log=self.log,
            known_recipients=frozenset({SUPERVISOR, "integration"}),
        )
        self.channel = SupervisorChannel(bus=self.bus, supervisor=SUPERVISOR)
        self.channel.attach()
        self.budget = BudgetManager(config.budget, config.provider)
        self.budget.set_scope_limits(
            "run",
            token_limit=config.budget.run_token_limit,
            cost_limit_usd=config.budget.run_cost_limit_usd,
        )
        if config.budget.per_provider_token_limit is not None:
            # A provider-wide ceiling, applied per provider scope.
            self.budget.set_scope_limits(
                f"provider:{config.provider.name}",
                token_limit=config.budget.per_provider_token_limit,
            )
        self.logger = StructuredLogger(
            level=config.observability.log_level,
            json_output=config.observability.log_json,
        ).bind(run_id=config.run_id)
        self.tracer = Tracer(
            service_name=config.observability.service_name,
            otlp_endpoint=config.observability.otlp_endpoint,
            sample_rate=config.observability.trace_sample_rate,
        )
        self.approver = ApprovalBroker(
            state=self.state,
            log=self.log,
            config=config.approval,
            run_id=config.run_id,
            bus=self.bus,
            auto_approve=auto_approve,
        )
        self._providers: list[ModelProvider] = []
        self._router: ProviderRouter | None = None
        self._injected_provider = provider
        self._role_providers: dict[str, ModelProvider] = {}
        self._record_exchanges = record_exchanges
        self.recorder: Any = None
        self._plan_version = 1
        self.owner_id: str = ""
        self.fence: int = 0
        self.cancellation: asyncio.Event | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self.protected_paths: tuple[str, ...] = tuple(
            config.protected_material_paths or ()
        )

    # ------------------------------------------------------------ providers

    def _provider_router(self) -> ProviderRouter:
        if self._router is None:
            configs = [self.config.provider, *self.config.fallback_providers]
            if self._injected_provider is not None:
                providers = [self._injected_provider, *build_providers(self.config)[1:]]
            else:
                providers = build_providers(self.config)
            if self._record_exchanges:
                from .replay.recorder import RecordingProvider

                self.recorder = RecordingProvider(
                    inner=providers[0],
                    store=self.artifacts,
                    run_id=self.config.run_id,
                )
                providers[0] = self.recorder
            self._router = ProviderRouter(providers, configs)
        return self._router

    def provider_for(self, role: str) -> Any:
        """The provider a role should use.

        A role may override the run's model through
        :class:`~agent_squad.config.RoleConfig`.  The override is applied
        by building a provider for that model once and reusing it, so a
        per-role model is not re-constructed per task.
        """
        override = self.config.roles.get(role)
        model_override = override.model_override if override else None
        if not model_override or model_override == self.config.provider.model:
            return self.provider
        cached = self._role_providers.get(role)
        if cached is None:
            from .providers.registry import build_provider

            config = self.config.provider.model_copy(
                update={"model": model_override}
            )
            cached = build_provider(config)
            self._role_providers[role] = cached
        return cached

    @property
    def provider(self) -> ModelProvider:
        return self._provider_router().primary

    # ----------------------------------------------------------------- run

    async def run(
        self,
        objective: str,
        *,
        description: str = "",
        request_spec: dict[str, Any] | None = None,
        integrate: bool = True,
        cancellation: asyncio.Event | None = None,
        plan_override: PlanResult | None = None,
    ) -> RunOutcome:
        """Execute a complete orchestration session."""
        started_at = _dt.datetime.now(_dt.UTC)
        warnings: list[str] = []
        with self.tracer.span(
            "run",
            run_id=self.config.run_id,
            objective=objective[:200],
            strategy_override=self.config.strategy_override,
        ):
            return await self._execute(
                objective,
                description=description,
                request_spec=request_spec,
                integrate=integrate,
                cancellation=cancellation,
                started_at=started_at,
                warnings=warnings,
                plan_override=plan_override,
            )

    async def _execute(
        self,
        objective: str,
        *,
        description: str,
        request_spec: dict[str, Any] | None,
        integrate: bool,
        cancellation: asyncio.Event | None,
        started_at: _dt.datetime,
        warnings: list[str],
        plan_override: PlanResult | None = None,
    ) -> RunOutcome:
        self.logger.info("run started", objective=objective[:120])
        self.cancellation = cancellation or asyncio.Event()

        # ---- durable ownership -------------------------------------------
        # Every run that mutates state takes exclusive ownership first, so
        # that two processes cannot drive the same run.  The fence is
        # incremented on takeover, and a stale owner's writes are refused.
        owner_id = f"{self.config.run_id}:{os.getpid()}"
        try:
            fence = self.state.acquire_run_ownership(
                self.config.run_id,
                owner_id=owner_id,
                owner_pid=os.getpid(),
                lease_s=self.config.scheduler.lease_ttl_s,
            )
        except ValidationError as exc:
            raise RunOwnershipError(
                f"another process is already running {self.config.run_id}: {exc.message}",
                run_id=self.config.run_id,
            ) from exc
        self.owner_id = owner_id
        self.fence = fence
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._heartbeat_task = asyncio.create_task(self._heartbeat_ownership())

        # The isolation profile is checked *before* anything runs: a profile
        # that promises a boundary must either have it or refuse to start.
        profile = get_profile(self.config.isolation_profile)
        isolation = IsolationGuard(profile, self.config.sandbox).check()
        self.logger.info(
            "isolation profile verified",
            profile=isolation.profile,
            boundaries=len(isolation.boundaries),
        )

        # One absolute deadline for the whole run, reserved time included.
        deadline = RunDeadline(
            total_s=self.config.deadline_s,
            reserve_s=self.config.teardown_reserve_s,
        )
        deadline.start()
        self.deadline = deadline

        self.state.create_run(
            self.config.run_id,
            config=self.config.to_dict(),
            metadata={"objective": objective, "description": description},
        )
        self.state.set_run_status(self.config.run_id, RunStatus.PLANNING)

        # ---- inspect ---------------------------------------------------
        with self.tracer.span("inspect", run_id=self.config.run_id):
            inspector = RepositoryInspector(self.config.repo_path)
            context = inspector.inspect()
        self.logger.debug(
            "repository inspected",
            files=len(context.files),
            tests=len(context.test_files),
            injection_findings=len(context.injection_findings),
        )

        # ---- the contract comes first, and it comes from the owner ------
        contract = build_contract(objective, request_spec, description=description)
        self.log.append(
            EventType.RUN_CREATED,
            run_id=self.config.run_id,
            payload={
                "contract": contract.to_dict(),
                "contract_hash": contract.contract_hash,
            },
        )

        # ---- plan -------------------------------------------------------
        plan: PlanResult | None = plan_override
        if plan is None:
            planner = build_planner(self.config, self.provider)
            with self.tracer.span("plan", run_id=self.config.run_id):
                request = PlanRequest(
                    run_id=self.config.run_id,
                    objective=objective,
                    description=description,
                    repo_path=self.config.repo_path,
                    base_revision=self.worktrees.head_revision()
                    if self.worktrees.has_commits()
                    else None,
                    context=context,
                    request_spec=request_spec,
                    contract=contract,
                )
                plan = await planner.plan(request)
        else:
            # A replay supplies the recorded plan, so the control plane is
            # exercised against exactly what the original run did.
            self.log.append(
                EventType.PLAN_CREATED,
                run_id=self.config.run_id,
                payload={
                    "planner": plan.planner,
                    "tasks": [t.model_dump(mode="json") for t in plan.tasks],
                    "briefs": {
                        k: v.model_dump(mode="json") for k, v in plan.briefs.items()
                    },
                    "rationale": plan.rationale,
                    "warnings": plan.warnings,
                    "replayed": True,
                },
            )
        warnings.extend(plan.warnings)
        self.logger.info(
            "plan created",
            planner=plan.planner,
            tasks=len(plan.tasks),
            warnings=len(plan.warnings),
        )
        self.log.append(
            EventType.PLAN_CREATED,
            run_id=self.config.run_id,
            payload={
                "planner": plan.planner,
                "tasks": [t.model_dump(mode="json") for t in plan.tasks],
                # The briefs are recorded too: without them a replay cannot
                # reconstruct what each worker was asked to do.
                "briefs": {k: v.model_dump(mode="json") for k, v in plan.briefs.items()},
                "rationale": plan.rationale,
                "warnings": plan.warnings,
            },
        )

        graph = plan.graph()
        for task in plan.tasks:
            self.state.upsert_task(task)

        # ---- strategy ----------------------------------------------------
        selector = StrategySelector(self.config.strategy)
        with self.tracer.span("strategy", run_id=self.config.run_id):
            decision = selector.select(
                graph,
                override=self.config.strategy_override,
                max_workers_override=self.config.max_workers_override,
                token_budget=self.config.budget.run_token_limit,
                cost_budget_usd=self.config.budget.run_cost_limit_usd,
                price_per_1k_tokens=(
                    self.config.provider.price_input_per_1k
                    + self.config.provider.price_output_per_1k
                ),
            )
        self.logger.info(
            "strategy selected",
            strategy=decision.strategy.value,
            workers=decision.worker_count,
            speedup=decision.estimated_parallel_speedup,
        )
        self.log.append(
            EventType.STRATEGY_SELECTED,
            run_id=self.config.run_id,
            payload=decision.model_dump(mode="json"),
        )
        self.state.set_strategy(self.config.run_id, decision.strategy.value)
        self.state.set_run_status(self.config.run_id, RunStatus.RUNNING)

        max_workers = min(
            decision.worker_count or self.config.scheduler.max_workers,
            self.config.scheduler.max_workers,
        )
        base_revision = (
            self.worktrees.head_revision() if self.worktrees.has_commits() else None
        )

        # ---- schedule ----------------------------------------------------
        runner = self._build_runner()
        scheduler = self._build_scheduler(
            graph,
            plan,
            runner=runner,
            max_workers=max_workers,
            base_revision=base_revision,
            cancellation=cancellation,
            deadline=deadline,
        )
        summary = await scheduler.run()
        outcomes = dict(scheduler.outcomes)

        # ---- bounded replanning -------------------------------------------
        plan, summary, outcomes = await self._maybe_replan(
            plan=plan,
            graph=graph,
            summary=summary,
            outcomes=outcomes,
            contract=contract,
            objective=objective,
            description=description,
            request_spec=request_spec,
            deadline=deadline,
            max_workers=max_workers,
            base_revision=base_revision,
            cancellation=cancellation,
        )
        graph = plan.graph()
        # Only genuinely new tasks are registered.  Re-registering an
        # existing task would emit a second TASK_CREATED event, which would
        # reset the task's state when the projection is rebuilt.
        known = {s.id for s in self.state.list_task_states(self.config.run_id)}
        for task in plan.tasks:
            if task.id not in known:
                self.state.upsert_task(task)

        for state in self.state.list_task_states(self.config.run_id):
            if state.id in outcomes:
                # Keep the scheduler's richer outcome but refresh the counters
                # from the durable state, which is the authoritative record.
                outcomes[state.id].tokens_used = state.tokens_used
                outcomes[state.id].cost_usd = state.cost_used_usd
                outcomes[state.id].attempts = state.attempt
                continue
            outcomes[state.id] = TaskOutcome(
                task_id=state.id,
                status=state.status,
                attempts=state.attempt,
                tokens_used=state.tokens_used,
                cost_usd=state.cost_used_usd,
            )

        # ---- integrate ----------------------------------------------------
        integration: IntegrationResult | None = None
        bundle: str | None = None
        verdict = None
        if integrate:
            with self.tracer.span("integration", run_id=self.config.run_id):
                integration, bundle = await self._integrate(
                    graph,
                    summary,
                    keep_workspace=bool(contract.required_outcomes),
                )
            verdict = await self._judge_contract(contract, integration)
            if integration is not None and integration.workspace and verdict is not None:
                # The candidate has been judged; the staging worktree is no
                # longer needed.
                self.worktrees.remove_path(integration.workspace)

        finished_at = _dt.datetime.now(_dt.UTC)
        # The repository may have moved on since the artifacts were produced
        # (for example because the integration committed into a worktree).
        # Artifacts that no longer describe the current tree are marked
        # invalid rather than silently reused.
        if base_revision:
            invalidated = self.state.invalidate_stale_artifacts(
                self.config.run_id, current_revision=base_revision
            )
            if invalidated:
                self.log.append(
                    EventType.ARTIFACT_PUBLISHED,
                    run_id=self.config.run_id,
                    payload={"invalidated": invalidated, "reason": "base revision changed"},
                )
        self.log.append(
            EventType.RUN_COMPLETED,
            run_id=self.config.run_id,
            payload={
                "status": summary.status.value,
                "wall_clock_s": summary.wall_clock_s,
            },
        )
        self.state.create_checkpoint(
            self.config.run_id, label="run-complete", metadata={"status": summary.status.value}
        )
        await self._release_ownership()

        return RunOutcome(
            run_id=self.config.run_id,
            status=summary.status,
            contract=verdict,
            summary=summary,
            decision=decision,
            plan=plan,
            outcomes=outcomes,
            integration=integration,
            patch_bundle=bundle,
            started_at=started_at,
            finished_at=finished_at,
            warnings=warnings,
        )

    # ----------------------------------------------------------- integration

    async def _heartbeat_ownership(self) -> None:
        """Keep the ownership lease alive while the run works.

        If the heartbeat ever fails -- because the ownership was taken
        over -- the run stops, because a process that has lost the run must
        not keep writing to it.
        """
        interval = max(1.0, self.config.scheduler.lease_ttl_s / 3)
        try:
            while True:
                await asyncio.sleep(interval)
                alive = self.state.heartbeat_run_ownership(
                    self.config.run_id, owner_id=self.owner_id, fence=self.fence
                )
                if not alive:
                    self.logger.error(
                        "run ownership was lost; stopping to avoid a split brain",
                        run_id=self.config.run_id,
                    )
                    if self.cancellation is not None:
                        self.cancellation.set()
                    return
        except asyncio.CancelledError:
            return

    async def _release_ownership(self) -> None:
        """Release the run so that another process may take it over."""
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task
            self._heartbeat_task = None
        if self.owner_id:
            with contextlib.suppress(Exception):
                self.state.release_run_ownership(
                    self.config.run_id, owner_id=self.owner_id, fence=self.fence
                )

    async def _judge_contract(
        self, contract: RequestContract, integration: IntegrationResult | None
    ) -> ContractVerdict | None:
        """Judge the run against its contract, on the *final* tree.

        This is the acceptance boundary.  It runs after integration, against
        the combined candidate, and it evaluates every required outcome
        against the base tree as well — so an outcome that was already true
        before the work started cannot be mistaken for evidence that the
        request was satisfied.
        """
        if integration is None or not integration.workspace:
            return None
        candidate = Path(integration.workspace)
        if not candidate.exists():
            return None

        # The base tree: the repository as it was before any work.
        base = self.worktrees.create(task_id=None, base_revision=integration.base_revision or None)
        try:
            gate = ContractGate(self.config, self.state, self.log)
            candidate_hash = self.worktrees.tree_of(integration.commit or "HEAD")
            verdict = await gate.verify(
                contract,
                base_tree=base.path,
                candidate_tree=candidate,
                base_revision=integration.base_revision,
                candidate_tree_hash=candidate_hash,
            )

            # The protected material: the files the worker was not allowed
            # to touch.  If one changed, the evidence is void.
            material = ProtectedMaterial(paths=self.protected_paths).capture(base.path)
            tamper = material.check(candidate)
            if any(v.state is OutcomeState.NOT_SATISFIED for v in tamper):
                verdict = ContractVerdict(
                    outcome=VerificationOutcome.REJECTED,
                    contract_hash=verdict.contract_hash,
                    base_revision=verdict.base_revision,
                    candidate_tree=verdict.candidate_tree,
                    verifier_version=verdict.verifier_version,
                    verdicts=verdict.verdicts + tamper,
                    commands=verdict.commands,
                    detail=(
                        "the run modified protected verification material, so the "
                        "evidence is void: "
                        + "; ".join(v.detail for v in tamper if v.detail)
                    ),
                )
        finally:
            self.worktrees.remove(base)
        self.logger.info(
            "contract judged",
            outcome=verdict.outcome.value,
            established=len(verdict.established),
            pre_satisfied=len(verdict.pre_satisfied),
            not_satisfied=len(verdict.not_satisfied),
        )
        return verdict

    def _build_runner(self) -> WorkerRunner:
        """Build the worker runner.

        A replanned run uses the same runner construction as the original,
        so the revision is not executed under different conditions.
        """
        return WorkerRunner(
            config=self.config,
            state=self.state,
            log=self.log,
            artifacts=self.artifacts,
            worktrees=self.worktrees,
            bus=self.bus,
            provider=self.provider,
            provider_resolver=self.provider_for,
            supervisor=SUPERVISOR,
            policy=self.policy,
            approver=self.approver.request,
            budget=self.budget,
            tracer=self.tracer,
        )

    def _build_scheduler(
        self,
        graph: TaskGraph,
        plan: PlanResult,
        *,
        runner: WorkerRunner,
        max_workers: int,
        base_revision: str | None,
        cancellation: Any,
        deadline: Any,
    ) -> Scheduler:
        """Build a scheduler for a plan.

        The initial run and a replanned run go through the same construction,
        so a revision is not executed under different conditions than the
        plan it replaced.
        """
        return Scheduler(
            config=self.config,
            graph=graph,
            state=self.state,
            log=self.log,
            runner=runner,
            gate=self.gate,
            briefs=plan.briefs,
            bus=self.bus,
            max_workers=max_workers,
            idempotency_prefix=self.config.run_id,
            base_revision=base_revision,
            cancellation=cancellation,
            tracer=self.tracer,
            logger=self.logger,
            deadline=deadline,
        )

    async def _maybe_replan(
        self,
        *,
        plan: PlanResult,
        graph: TaskGraph,
        summary: RunSummary,
        outcomes: dict[str, TaskOutcome],
        contract: RequestContract,
        objective: str,
        description: str,
        request_spec: dict[str, Any] | None,
        deadline: Any,
        max_workers: int,
        base_revision: str | None,
        cancellation: Any,
    ) -> tuple[PlanResult, RunSummary, dict[str, TaskOutcome]]:
        """Revise the plan once, if a classified failure justifies it.

        Replanning is only attempted for a *verification* failure — the
        kind that says "the work did not meet the requirement" — and only
        while the budget allows.  A model or infrastructure failure is a
        different problem, and revising the plan would not address it.
        """
        max_replans = self.config.scheduler.max_replans
        if max_replans <= 0:
            return plan, summary, outcomes

        failed = [
            task_id
            for task_id, outcome in outcomes.items()
            if outcome.status is TaskStatus.FAILED
            and outcome.error_class == "verification"
        ]
        if not failed:
            return plan, summary, outcomes

        task_id = sorted(failed)[0]
        outcome = outcomes[task_id]
        verified = {
            t.id: t for t in plan.tasks if outcomes.get(t.id) and outcomes[t.id].status is TaskStatus.SUCCEEDED
        }

        context = RevisionContext(
            objective=objective,
            description=description,
            failed_task_id=task_id,
            failure_reason=outcome.error or "the acceptance criteria were not met",
            failed_criteria=tuple(
                outcome.verification.failed if outcome.verification else ()
            ),
            completed_task_ids=tuple(sorted(verified)),
            current_plan=tuple(t.model_dump(mode="json") for t in plan.tasks),
        )

        try:
            revised_plan = await self._request_revision(context, contract)
        except Exception as exc:
            self.logger.info(
                "replanning was refused", task_id=task_id, error=str(exc)[:200]
            )
            return plan, summary, outcomes

        changed = check_revision(
            previous=tuple(plan.tasks),
            revised=tuple(revised_plan.tasks),
            contract=contract,
            completed_task_ids=tuple(sorted(verified)),
        )
        merged = merge_revisions(
            previous=tuple(plan.tasks),
            revised=tuple(revised_plan.tasks),
            completed=verified,
        )
        revised_plan = revised_plan.model_copy(
            update={
                "tasks": list(merged),
                "briefs": {
                    **revised_plan.briefs,
                    **{
                        t.id: brief
                        for t, brief in zip(merged, plan.briefs.values(), strict=False)
                        if t.id not in revised_plan.briefs
                    },
                },
            }
        )

        version = self._plan_version + 1
        self._plan_version = version
        self.log.append(
            EventType.PLAN_REVISED,
            run_id=self.config.run_id,
            task_id=task_id,
            payload={
                "version": version,
                "reason": context.failure_reason,
                "failed_criteria": list(context.failed_criteria),
                "changed_task_ids": list(changed),
                "preserved_task_ids": list(sorted(verified)),
            },
        )
        self.logger.info(
            "plan revised",
            version=version,
            changed=list(changed),
            preserved=list(sorted(verified)),
        )

        # Run the revised plan.  The verified work is not repeated.
        revised_graph = revised_plan.graph()
        runner = self._build_runner()
        scheduler = self._build_scheduler(
            revised_graph,
            revised_plan,
            runner=runner,
            max_workers=max_workers,
            base_revision=base_revision,
            cancellation=cancellation,
            deadline=deadline,
        )
        new_summary = await scheduler.run()
        new_outcomes = dict(scheduler.outcomes)
        for state in self.state.list_task_states(self.config.run_id):
            if state.id in new_outcomes:
                new_outcomes[state.id].tokens_used = state.tokens_used
                new_outcomes[state.id].cost_usd = state.cost_used_usd
                new_outcomes[state.id].attempts = state.attempt
        return revised_plan, new_summary, new_outcomes

    async def _request_revision(
        self, context: RevisionContext, contract: RequestContract
    ) -> PlanResult:
        """Ask the planner for a revised plan."""
        from ..planning.planner import PlanRequest

        planner = build_planner(self.config, self.provider)
        request = PlanRequest(
            run_id=self.config.run_id,
            objective=context.objective,
            description=(
                f"{context.description}\n\nThe previous attempt failed.\n"
                f"{context.to_prompt()}"
            ),
            repo_path=self.config.repo_path,
            base_revision=(
                self.worktrees.head_revision() if self.worktrees.has_commits() else None
            ),
            contract=contract,
        )
        return await planner.plan(request)

    async def _integrate(
        self,
        graph: TaskGraph,
        summary: RunSummary,
        *,
        keep_workspace: bool = False,
    ) -> tuple[IntegrationResult | None, str | None]:
        patches: list[PatchArtifact] = []
        for state in self.state.list_task_states(self.config.run_id):
            if state.status is not TaskStatus.SUCCEEDED:
                continue
            artifacts = self.state.list_artifacts(self.config.run_id, state.id)
            for artifact in artifacts:
                if artifact.kind is not ArtifactKind.PATCH:
                    continue
                try:
                    content = self.artifacts.read_text(artifact)
                except Exception:
                    continue
                patches.append(
                    PatchArtifact(
                        artifact_id=artifact.id,
                        task_id=state.id,
                        base_revision=artifact.provenance.base_revision or "",
                        sha256=artifact.sha256,
                        content=content,
                        files=tuple(
                            str(entry) for entry in (artifact.metadata.get("files") or [])
                        ),
                        producer=artifact.provenance.producer,
                        head_revision=artifact.metadata.get("head_revision"),
                        base_tree=str(artifact.metadata.get("base_tree") or ""),
                        head_tree=str(artifact.metadata.get("head_tree") or ""),
                    )
                )
        if not patches:
            # No patches is not "nothing to judge": it is the no-op case, and
            # the contract gate must still see the candidate tree so that a
            # pre-satisfied outcome is reported as *inconclusive* rather than
            # as success.
            base = self.worktrees.head_revision() if self.worktrees.has_commits() else ""
            engine = IntegrationEngine(
                config=self.config, worktrees=self.worktrees, log=self.log
            )
            result = await engine.integrate(
                [],
                base_revision=base,
                run_id=self.config.run_id,
                keep_workspace=keep_workspace,
            )
            bundle = engine.build_patch_bundle([], base_revision=base)
            return result, bundle

        # Patches must be applied in dependency order: a dependent task's
        # patch was computed against its dependency's head.
        order = {task_id: index for index, task_id in enumerate(graph.topological_order())}
        patches.sort(key=lambda p: order.get(p.task_id, 0))

        engine = IntegrationEngine(config=self.config, worktrees=self.worktrees, log=self.log)
        base = self.worktrees.head_revision() if self.worktrees.has_commits() else ""
        dependencies = {task.id: task.depends_on for task in graph.tasks.values()}
        result = await engine.integrate(
            patches,
            base_revision=base,
            run_id=self.config.run_id,
            dependencies=dependencies,
            keep_workspace=keep_workspace,
        )
        bundle = engine.build_patch_bundle(patches, base_revision=base)
        bundle_path = self.paths.root / "patch-bundle.diff"
        bundle_path.write_text(bundle, encoding="utf-8")
        return result, bundle

    # ------------------------------------------------------------- teardown

    async def aclose(self) -> None:
        for provider in self._providers:
            await provider.aclose()
        self.db.close()

    def close(self) -> None:
        """Close the run's resources synchronously.

        Provided for callers that are not running inside an event loop (the
        CLI's synchronous sub-commands and some tests).
        """
        self.db.close()

    # --------------------------------------------------------------- report

    def run_report(self, run_id: str) -> dict[str, Any]:
        """A structured report of a stored run."""
        run = self.state.get_run(run_id) or {}
        tasks = self.state.list_task_states(run_id)
        events = self.log.read(run_id)
        return {
            "run": run,
            "tasks": [t.model_dump(mode="json") for t in tasks],
            "event_count": len(events),
            "artifacts": [
                a.model_dump(mode="json") for a in self.state.list_artifacts(run_id)
            ],
            "pending_approvals": self.state.pending_approvals(run_id),
            "supervisor": self.channel.report(),
            "messages": self.state.list_messages(run_id),
        }
