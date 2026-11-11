"""Typed, validated configuration for the orchestrator.

Configuration is data, not code: every knob that affects scheduling, budgets,
sandboxing or provider behaviour lives here so that a run can be reproduced
exactly from a single serialisable object.  The object is stored alongside the
run and hashed into the replay manifest.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import ValidationError


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderConfig(_Base):
    """Connection + pricing configuration for a single model endpoint."""

    name: str = Field(description="Registered provider name, e.g. mock/openai_compat")
    model: str = "default"
    base_url: str | None = None
    api_key_env: str | None = None
    max_concurrency: int = Field(default=4, ge=1)
    timeout_s: float = Field(default=120.0, gt=0)
    max_output_tokens: int = Field(default=4096, ge=16)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    seed: int | None = None
    price_input_per_1k: float = Field(default=0.0, ge=0)
    price_output_per_1k: float = Field(default=0.0, ge=0)
    price_currency: str = "USD"

    def api_key(self) -> str | None:
        if not self.api_key_env:
            return None
        return os.environ.get(self.api_key_env)


class RoleConfig(_Base):
    """Per-role limits, capabilities and model overrides."""

    enabled: bool = True
    capabilities: tuple[str, ...] = ()
    max_iterations: int = Field(default=12, ge=1, le=200)
    max_turn_tokens: int = Field(default=60_000, ge=256)
    model_override: str | None = None
    max_parallel: int = Field(default=1, ge=1)
    temperature: float | None = None


class SandboxConfig(_Base):
    """Limits applied to untrusted command execution.

    These are *local* limits.  They are a meaningful defence-in-depth layer on
    a single host but they are explicitly **not** a security boundary: a
    determined attacker with code execution in the sandboxed process can
    attempt to escape.  See ``docs/security-threat-model.md``.
    """

    allow_network: bool = False
    cpu_time_s: int = Field(default=60, ge=1)
    memory_mb: int = Field(default=2048, ge=64)
    wall_time_s: float = Field(default=120.0, gt=0)
    max_output_bytes: int = Field(default=1_000_000, ge=1024)
    max_processes: int = Field(default=64, ge=1)
    allowed_binaries: tuple[str, ...] = (
        "python3",
        "python",
        "pytest",
        "git",
        "ls",
        "cat",
        "echo",
        "sed",
        "grep",
        "find",
        "diff",
        "true",
        "false",
        "sh",
        "bash",
    )
    env_allowlist: tuple[str, ...] = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "VIRTUAL_ENV")
    strip_env: bool = True


class WorkspaceConfig(_Base):
    worktree_root: str | None = None
    keep_worktrees_on_success: bool = False
    keep_worktrees_on_failure: bool = True
    integration_root: str | None = None


class BudgetConfig(_Base):
    """Monetary and token ceilings."""

    run_token_limit: int | None = Field(default=None, ge=1)
    run_cost_limit_usd: float | None = Field(default=None, gt=0)
    per_task_token_limit: int | None = Field(default=None, ge=1)
    per_task_cost_limit_usd: float | None = Field(default=None, gt=0)
    per_provider_token_limit: int | None = Field(default=None, ge=1)
    context_window_tokens: int = Field(default=128_000, ge=1024)


class RetryConfig(_Base):
    max_attempts: int = Field(default=3, ge=1, le=20)
    initial_backoff_s: float = Field(default=0.5, ge=0)
    max_backoff_s: float = Field(default=30.0, ge=0)
    backoff_multiplier: float = Field(default=2.0, ge=1.0)
    jitter: float = Field(default=0.25, ge=0.0, le=1.0)

    def backoff_for(self, attempt: int, jitter_factor: float = 1.0) -> float:
        """Exponential backoff for ``attempt`` (1-based) with proportional jitter.

        ``jitter_factor`` is a deterministic value in ``[0, 1]`` supplied by
        the caller.  Deterministic jitter keeps replay reproducible while
        still de-synchronising concurrent workers.
        """
        exponent = max(0, attempt - 1)
        base = self.initial_backoff_s * (self.backoff_multiplier**exponent)
        base = min(base, self.max_backoff_s)
        span = base * self.jitter
        return max(0.0, base - span * jitter_factor)


class SchedulerConfig(_Base):
    max_workers: int = Field(default=4, ge=1, le=64)
    default_task_timeout_s: float = Field(default=900.0, gt=0)
    lease_ttl_s: float = Field(default=300.0, gt=0)
    poll_interval_s: float = Field(default=0.05, gt=0)
    max_delegation_depth: int = Field(default=2, ge=0, le=5)
    stall_detection_s: float = Field(default=300.0, gt=0)
    lease_ttl_s: float = Field(
        default=300.0,
        gt=0,
        description=(
            "How long a run's ownership lease lasts without a heartbeat.  "
            "A lease that has expired may be taken over, which fences the "
            "previous owner out."
        ),
    )
    max_replans: int = Field(
        default=1,
        ge=0,
        le=10,
        description=(
            "How many times a run may revise its plan after a classified "
            "verification failure.  Zero disables replanning; the bound stops "
            "a plan that keeps changing from churning."
        ),
    )


class StrategyConfig(_Base):
    """Thresholds for the adaptive parallelism decision engine."""

    min_tasks_for_parallel: int = Field(default=2, ge=1)
    max_workers: int = Field(default=4, ge=1, le=32)
    coordination_overhead_tokens: int = Field(default=1200, ge=0)
    context_duplication_tokens: int = Field(default=1500, ge=0)
    integration_risk_penalty: float = Field(default=0.35, ge=0.0, le=1.0)
    parallel_speedup_exponent: float = Field(default=0.8, ge=0.1, le=1.5)
    shared_file_conflict_weight: float = Field(default=0.5, ge=0.0)


class VerificationConfig(_Base):
    python_executable: str = "python3"
    command_timeout_s: float = Field(default=600.0, gt=0)
    require_independent_reexecution: bool = True
    integration_test_command: tuple[str, ...] = ("python3", "-m", "pytest", "-q")
    review_is_advisory: bool = True
    allow_llm_only_acceptance: bool = False


class ObservabilityConfig(_Base):
    log_level: str = "INFO"
    log_json: bool = False
    otlp_endpoint: str | None = None
    service_name: str = "agent-squad"
    trace_sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)


class ApprovalConfig(_Base):
    """Protected actions that always require an explicit human decision."""

    require_approval_for_git_commit: bool = True
    require_approval_for_git_push: bool = True
    require_approval_for_network: bool = True
    require_approval_for_delete: bool = True
    protected_paths: tuple[str, ...] = ()
    approval_timeout_s: float = Field(default=3600.0, gt=0)


class RunConfig(_Base):
    """The complete, serialisable configuration of a single run."""

    run_id: str
    repo_path: str
    provider: ProviderConfig = Field(
        default_factory=lambda: ProviderConfig(name="mock", model="mock-deterministic-v1")
    )
    fallback_providers: tuple[ProviderConfig, ...] = ()
    roles: dict[str, RoleConfig] = Field(default_factory=dict)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    verification: VerificationConfig = Field(default_factory=VerificationConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
    data_dir: str | None = None
    strategy_override: str | None = None
    max_workers_override: int | None = Field(default=None, ge=1, le=64)
    deadline_s: float | None = Field(
        default=None,
        gt=0,
        description=(
            "An absolute deadline for the whole run, covering inspection, "
            "planning, worker requests, verification, retries and "
            "integration.  A per-request timeout is not a whole-run deadline: "
            "a run that makes many requests can exceed any single request's "
            "limit while never breaching one."
        ),
    )
    protected_material_paths: tuple[str, ...] = Field(
        default=(),
        description=(
            "Repository paths the worker must not be able to change -- the "
            "hidden regression checks, the baseline tests, the configuration. "
            "They are excluded from the write scope and their content hashes "
            "are checked before the verdict is issued.  This is distinct from "
            "ApprovalConfig.protected_paths, which means 'writes here always "
            "need a human'."
        ),
    )
    isolation_profile: str = Field(
        default="development",
        description=(
            "The isolation profile the run executes under.  'development' is "
            "best-effort containment; 'hardened' refuses to run unless the "
            "boundaries it promises can actually be established."
        ),
    )
    teardown_reserve_s: float = Field(
        default=5.0,
        ge=0,
        description=(
            "Time reserved at the end of the deadline for cleanup, so that "
            "the run stops with time left to reclaim what it owns."
        ),
    )

    @field_validator("roles")
    @classmethod
    def _validate_roles(cls, v: dict[str, RoleConfig]) -> dict[str, RoleConfig]:
        if not v:
            return v
        for name in v:
            if not name.strip():
                raise ValueError("role names must be non-empty")
        return v

    @model_validator(mode="after")
    def _validate_overrides(self) -> RunConfig:
        if (
            self.max_workers_override is not None
            and self.max_workers_override > self.strategy.max_workers
        ):
            raise ValueError(
                "max_workers_override exceeds strategy.max_workers "
                f"({self.max_workers_override} > {self.strategy.max_workers})"
            )
        return self

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def load_config(path: str | Path) -> dict[str, Any]:
    """Load a TOML configuration file into a plain dict."""
    p = Path(path)
    if not p.exists():
        raise ValidationError(f"config file not found: {p}", path=str(p))
    try:
        with p.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - depends on user file
        raise ValidationError(f"invalid TOML in {p}: {exc}", path=str(p)) from exc


def build_run_config(data: dict[str, Any], *, repo_path: str, run_id: str) -> RunConfig:
    """Construct a :class:`RunConfig` from a (possibly partial) mapping.

    Unknown top-level keys are rejected so typos in a config file fail loudly
    instead of silently disabling a safety control.
    """
    payload = dict(data)
    payload["repo_path"] = repo_path
    payload["run_id"] = run_id
    try:
        return RunConfig.model_validate(payload)
    except Exception as exc:  # pydantic ValidationError
        raise ValidationError(f"invalid run configuration: {exc}") from exc


StrategyName = Literal["single", "static_two", "static_four", "adaptive", "dag"]
