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
import json
import os
import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agents.approval import ApprovalBroker
from .agents.messaging import MessageBus, SupervisorChannel
from .agents.runner import WorkerRunner
from .artifacts.store import ArtifactStore
from .config import RunConfig
from .dag.graph import TaskGraph
from .errors import InfrastructureError, RunOwnershipError, ValidationError
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
from .providers.ledger import RequestLedger
from .providers.registry import ProviderRouter, build_providers
from .runtime.deadline import (
    DeadlineExceeded,
    RunDeadline,
    bind_run_scope,
    unbind_run_scope,
)
from .runtime.registry import ResourceRegistry, reconcile
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
    requests: dict[str, Any] | None = None
    """The request accounting record: every dispatched attempt, its phase,
    its outcome and its usage.  Reconciles exactly with the attempts the
    run made."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status.value,
            "summary": self.summary.model_dump(mode="json"),
            "decision": self.decision.model_dump(mode="json") if self.decision else None,
            "tasks": {k: v.to_dict() for k, v in self.outcomes.items()},
            "integration": self.integration.to_dict() if self.integration else None,
            "contract": self.contract.to_dict() if self.contract else None,
            "requests": self.requests,
            "warnings": list(self.warnings),
        }


class Orchestrator:
    """Runs one orchestration session end to end."""

    @staticmethod
    def _requested_containment(config: RunConfig) -> str:
        """Which containment backend the profile requires.

        The hardened profile promises a filesystem boundary, so it requires
        bubblewrap; the development profile promises nothing beyond
        resource limits, so it runs uncontained unless the operator asked
        for containment explicitly.
        """
        explicit = (config.sandbox.containment or "none").strip().lower()
        if explicit not in ("", "none"):
            return explicit
        if config.isolation_profile == "hardened":
            return "bubblewrap"
        return "none"

    def _git_dir(self, repo_path: str | Path) -> str:
        """The repository's git metadata directory.

        A git worktree is unusable without it -- the worktree's ``.git``
        file points into it -- so it is bound into the containment
        boundary.  It is in the same trust domain as the workspace.
        """
        candidate = Path(repo_path) / ".git"
        return str(candidate) if candidate.exists() else ""

    def _with_sandbox(
        self,
        config: RunConfig,
        *,
        private_home: str,
        containment: str,
    ) -> RunConfig:
        """Return the effective configuration for this run.

        The sandbox configuration is frozen, so the effective values are
        produced with an explicit copy and the result is what gets
        persisted and replayed.
        """
        binds = tuple(
            dict.fromkeys(
                (*config.sandbox.extra_bind_paths, self._git_dir(config.repo_path))
            )
        )
        sandbox = config.sandbox.model_copy(
            update={
                "private_home": private_home,
                "containment": containment,
                "extra_bind_paths": tuple(p for p in binds if p),
                "registry_path": str(self.paths.root / "resources.json"),
            }
        )
        return config.model_copy(update={"sandbox": sandbox})

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

        # Every run gets a private home.  It is created before anything
        # executes so that no command can inherit the invoking user's
        # home, and it is removed when the run tears down.
        self.private_home = self.paths.root / "sandbox" / "home"
        self.private_home.mkdir(parents=True, exist_ok=True)
        os.chmod(self.private_home, 0o700)
        for sub in (".config", ".cache", ".local", "tmp"):
            (self.private_home / sub).mkdir(parents=True, exist_ok=True)
        self.config = self._with_sandbox(
            config,
            private_home=str(self.private_home),
            containment=self._requested_containment(config),
        )
        config = self.config

        # Persist the configuration so that the run can be replayed later
        # without the operator having to reconstruct it.
        (self.paths.root / "run-config.json").write_text(
            config.model_dump_json(indent=2), encoding="utf-8"
        )

        self.db = Database(self.paths.database)
        self.log = EventLog(self.db)
        self.state = StateStore(self.db, self.log)
        # The run's durable resource registry.  While a sandboxed command
        # is alive its process group is recorded here, so that a run
        # killed with SIGKILL leaves a record that the next run reclaims.
        # Load the previous owner's registry before reclaiming it.  A fresh,
        # empty registry here would make reconciliation a no-op -- which is
        # exactly the bug this test was written to catch.
        self.registry = ResourceRegistry.load(self.paths.root / "resources.json")
        # Every model attempt is recorded here, including planner, worker,
        # reviewer, retry and fallback attempts, and including failures and
        # cancellations.  The ledger is the run's accounting record: a run
        # that cannot account for its own requests cannot report its cost.
        self.request_ledger = RequestLedger()
        # A previous owner may have been killed and left processes behind.
        # Reclaiming them now, before this run starts any work, is what
        # makes the next run safe to start.
        self.reconciliation = reconcile(self.registry, grace_s=self.config.teardown_reserve_s)
        if self.reconciliation.survivors or self.reconciliation.processes_terminated:
            self.log.append(
                EventType.RUN_RECONCILED,
                run_id=self.config.run_id,
                payload={
                    "processes_terminated": self.reconciliation.processes_terminated,
                    "force_killed": self.reconciliation.processes_force_killed,
                    "survivors": list(self.reconciliation.survivors),
                },
            )
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
                providers = [
                self._injected_provider,
                *build_providers(self.config, ledger=self.request_ledger)[1:],
            ]
            else:
                providers = build_providers(self.config, ledger=self.request_ledger)
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
            cached = build_provider(config, ledger=self.request_ledger)
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
        # SIGINT and SIGTERM become a graceful cancellation, so that a
        # signalled run still reclaims the children and workspaces it owns.
        token = cancellation or asyncio.Event()
        restore_signals = self._install_signal_handlers(token)
        try:
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
                    cancellation=token,
                    started_at=started_at,
                    warnings=warnings,
                    plan_override=plan_override,
                )
        finally:
            restore_signals()
            # A bound run scope that outlives its run would leak the run's
            # cancellation token into the next one -- the next run would
            # start already-cancelled.  The scope is released with the run.
            unbind_run_scope()

    def _install_signal_handlers(
        self, cancellation: asyncio.Event
    ) -> Callable[[], None]:
        """Turn SIGINT and SIGTERM into a graceful cancellation.

        A signal that kills the process outright cannot reclaim what the
        run owns: the sandboxed children, the worktrees and the private
        home are all left behind.  Intercepting the signal and setting the
        cancellation token lets the normal teardown run instead.

        A **second** signal restores the default disposition and re-raises
        it, so an operator can still stop a run that is stuck in a way the
        cooperative path cannot resolve.  That is the escape hatch, and it
        is deliberate: a handler that swallows every signal would make a
        wedged run unkillable except with SIGKILL.
        """
        if threading.current_thread() is not threading.main_thread():
            return lambda: None
        previous: dict[int, Any] = {}
        loop = asyncio.get_running_loop()

        def handler(signum: int, frame: Any) -> None:
            if cancellation.is_set():
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(signum, signal.SIG_DFL)
                os.kill(os.getpid(), signum)
                return
            # A signal handler runs while the loop is inside ``select``.
            # Setting the event directly resolves its waiter, but it does
            # not interrupt ``select`` -- the loop only notices when the
            # sleep it is blocked on expires, which can be the task's whole
            # timeout.  ``call_soon_threadsafe`` writes the loop's
            # self-pipe, which is what actually wakes it.
            try:
                loop.call_soon_threadsafe(cancellation.set)
            except RuntimeError:  # pragma: no cover - loop already closed
                cancellation.set()
            self.logger.warning(
                "run interrupted by signal; cancelling gracefully",
                signal=signal.Signals(signum).name,
            )

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                previous[sig] = signal.getsignal(sig)
                signal.signal(sig, handler)
            except (ValueError, OSError):  # pragma: no cover - not the main thread
                continue

        def restore() -> None:
            for sig, old in previous.items():
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(sig, old)

        return restore

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
        # The pre-scheduler phases and the synchronous seam read cancellation
        # from the ambient scope; bind the run's token so a signal reaches
        # them and not only the tasks that already have the token.
        bind_run_scope(cancellation=self.cancellation, run_id=self.config.run_id)

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
        self._heartbeat_task = asyncio.create_task(self._heartbeat_ownership())

        # The isolation profile is checked *before* anything runs: a profile
        # that promises a boundary must either have it or refuse to start.
        profile = get_profile(self.config.isolation_profile)
        guard = IsolationGuard(
            profile,
            self.config.sandbox,
            workspace=self.paths.worktrees,
            private_home=self.private_home,
        )
        isolation = guard.check()
        self.isolation = isolation
        self.logger.info(
            "isolation profile verified",
            profile=isolation.profile,
            boundaries=len(isolation.boundaries),
        )
        # The hardened profile's boundaries are *demonstrated*, not claimed:
        # a real command is executed inside the boundary and its
        # observations decide whether the profile is honoured.
        probe = await guard.probe()
        if probe is not None:
            self.logger.info(
                "containment probed",
                backend=probe.backend,
                ok=probe.ok,
                checks=",".join(
                    name for name, passed in probe.checks.items() if not passed
                )
                or "all boundaries held",
            )
            if not probe.ok:
                raise InfrastructureError(
                    "the isolation profile promised boundaries that the "
                    "containment probe did not observe: "
                    + ", ".join(
                        name for name, passed in probe.checks.items() if not passed
                    )
                    + f". Refusing to run. ({probe.detail})",
                    profile=self.config.isolation_profile,
                    detail=probe.detail,
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
        deadline_exceeded: DeadlineExceeded | None = None
        if integrate:
            try:
                with self.tracer.span("integration", run_id=self.config.run_id):
                    integration, bundle = await self._integrate(
                        graph,
                        summary,
                        keep_workspace=bool(contract.required_outcomes),
                    )
            except DeadlineExceeded as exc:
                # A run that ran out of time is an outcome, not a crash.
                # The integration phase is refused before it starts when
                # the budget is exhausted, and nothing it would have
                # created exists.
                deadline_exceeded = exc
                self.log.append(
                    EventType.RUN_CANCELLED,
                    run_id=self.config.run_id,
                    payload={
                        "reason": "deadline exceeded",
                        "phase": getattr(exc, "phase", "integration"),
                        "deadline": self.deadline.summary()
                        if getattr(self, "deadline", None)
                        else {},
                    },
                )
            if deadline_exceeded is None:
                verdict = await self._judge_contract(contract, integration)
                if (
                    integration is not None
                    and integration.workspace
                    and verdict is not None
                ):
                    # The candidate has been judged; the staging worktree is
                    # no longer needed.
                    self.worktrees.remove_path(integration.workspace)
            else:
                verdict = ContractVerdict(
                    outcome=VerificationOutcome.TIMED_OUT,
                    contract_hash=contract.contract_hash,
                    base_revision=base_revision or "",
                    verifier_version="contract-gate/1",
                    detail=(
                        "the run exceeded its absolute deadline before the "
                        "candidate could be integrated and judged: "
                        + str(getattr(deadline_exceeded, "phase", "integration"))
                    ),
                )

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
        # A run that ran out of time is reported as cancelled even when
        # every task it managed to run succeeded: the request was not
        # judged, and reporting success would claim an acceptance that was
        # never issued.
        final_status = (
            RunStatus.CANCELLED if deadline_exceeded is not None else summary.status
        )
        self.log.append(
            EventType.RUN_COMPLETED,
            run_id=self.config.run_id,
            payload={
                "status": final_status.value,
                "wall_clock_s": summary.wall_clock_s,
            },
        )
        self.state.create_checkpoint(
            self.config.run_id,
            label="run-complete",
            metadata={"status": final_status.value},
        )
        await self._release_ownership()

        return RunOutcome(
            run_id=self.config.run_id,
            status=final_status,
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
            requests=self.request_ledger.summary(),
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
        from .planning.planner import PlanRequest

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
        self._reclaim_resources()
        self._reclaim_workspaces()
        self._remove_private_home()

    def _reclaim_workspaces(self) -> None:
        """Remove every workspace the run did not explicitly retain.

        The scheduler disposes of a worktree after a *completed* attempt,
        but an attempt that timed out or raised never produces a result to
        dispose of, so its worktree would otherwise survive the run.  The
        run's workspace root is swept here as the last line of defence:
        a directory survives only when the owner opted into retention and
        the durable manifest records it.
        """
        root = getattr(self, "paths", None)
        if root is None or not root.worktrees.exists():
            return
        retained: set[str] = set()
        manifest_path = self.paths.root / "retained-workspaces.json"
        if manifest_path.exists():
            try:
                for entry in json.loads(manifest_path.read_text(encoding="utf-8")):
                    retained.add(str(entry.get("path")))
            except (OSError, json.JSONDecodeError, AttributeError):
                retained = set()
        for child in sorted(self.paths.worktrees.iterdir()):
            if not child.is_dir():
                continue
            if str(child) in retained:
                continue
            self.worktrees.remove_path(child)

    def _reclaim_resources(self) -> None:
        """Reclaim everything this run still owns.

        A run that ends normally has usually finished its commands, but a
        run that was cancelled, failed or interrupted may not have, and a
        survivor is worse than a slow teardown.  The grace period is the
        policy's five seconds: terminate, wait, escalate to ``SIGKILL``,
        and report whatever is still alive rather than pretending.
        """
        registry = getattr(self, "registry", None)
        if registry is None:
            return
        # Reconcile the shared instance: the sandbox registers commands
        # while they are alive, and the teardown is the component that
        # reclaims anything the cancellation path could not.
        report = registry.cleanup(grace_s=5.0)
        if report.survivors:
            self.logger.error(
                "resources survived the run's teardown",
                survivors=list(report.survivors),
            )

    def _remove_private_home(self) -> None:
        """Delete the run's private home.

        The private home is where a sandboxed command could have written
        anything it liked, so it is removed with the run rather than left
        behind for the next one to inherit.
        """
        import shutil

        home = getattr(self, "private_home", None)
        if home is None:
            return
        shutil.rmtree(home, ignore_errors=True)

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
