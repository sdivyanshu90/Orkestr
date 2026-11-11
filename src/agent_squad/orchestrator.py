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
import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agents.approval import ApprovalBroker
from .agents.messaging import MessageBus, SupervisorChannel
from .agents.runner import WorkerRunner
from .artifacts.store import ArtifactStore
from .config import RunConfig
from .dag.graph import TaskGraph
from .integration.engine import IntegrationEngine
from .integration.patch import IntegrationResult, PatchArtifact
from .models.artifact import ArtifactKind
from .models.contract import ContractVerdict, RequestContract
from .models.run import RunStatus, RunSummary, StrategyDecision
from .models.task import TaskStatus
from .observe.events import StructuredLogger
from .observe.tracing import Tracer
from .planning.contract import build_contract
from .planning.inspector import RepositoryInspector
from .planning.planner import PlanRequest, PlanResult, build_planner
from .providers.base import ModelProvider
from .providers.budget import BudgetManager
from .providers.registry import ProviderRouter, build_providers
from .scheduler.engine import Scheduler, TaskOutcome
from .security.policy import PolicyEngine
from .store.db import Database
from .store.events import EventLog, EventType
from .store.state import StateStore
from .strategy.selector import StrategySelector
from .verification.contract_gate import ContractGate
from .verification.gate import VerificationGate
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
    ) -> RunOutcome:
        self.logger.info("run started", objective=objective[:120])

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
        runner = WorkerRunner(
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
        scheduler = Scheduler(
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
        )
        summary = await scheduler.run()
        outcomes = dict(scheduler.outcomes)
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
