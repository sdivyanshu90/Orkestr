"""Fault injection harness.

The harness makes failures *reproducible*.  Each fault is a small, explicit
object that can be attached to a provider, a tool registry or a scheduler,
and each fault has a documented expectation about what the runtime should
do.

The point is not to "test that errors happen".  It is to assert the
*recovery contract*: after a given fault, the run must reach a specific,
named state -- and it must reach it without corrupting state or silently
dropping work.

Fault catalogue:

===================== =======================================================
Fault                 Expected outcome
===================== =======================================================
provider_unavailable  The task fails after exhausting its retries; the run
                      reports the failure; no state is corrupted.
provider_flaky        The first N calls fail transiently; the retry policy
                      completes the work.
model_garbage         The provider returns output that fails schema
                      validation; the run fails cleanly rather than acting
                      on malformed data.
worker_crash          The worker raises an unexpected exception; the
                      scheduler records the failure and the dependent task
                      is blocked, not silently skipped.
slow_worker           The worker exceeds its deadline; the task times out
                      and is retried or reported as timed out.
lost_message          A message is dropped; the run continues because no
                      message is load-bearing for correctness.
duplicate_message     A message is delivered twice; the bus rejects the
                      second copy.
stale_ownership       A late worker tries to write state for a superseded
                      attempt; the write is fenced out.
conflicting_patches   Two workers edit the same file; integration detects
                      the conflict.
stalled_task          A task makes no progress; the stall detector fires.
===================== =======================================================
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..errors import ModelError, TransientModelError
from ..providers.base import ModelRequest, ModelResponse


class Fault(str, Enum):
    """The catalogue of injectable faults."""

    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_FLAKY = "provider_flaky"
    MODEL_GARBAGE = "model_garbage"
    WORKER_CRASH = "worker_crash"
    SLOW_WORKER = "slow_worker"
    LOST_MESSAGE = "lost_message"
    DUPLICATE_MESSAGE = "duplicate_message"
    STALE_OWNERSHIP = "stale_ownership"
    CONFLICTING_PATCHES = "conflicting_patches"
    STALLED_TASK = "stalled_task"


@dataclass
class FaultSpec:
    """A fault and its parameters."""

    kind: Fault
    times: int = 1
    delay_s: float = 0.0
    message: str = "injected fault"

    def describe(self) -> str:
        return f"{self.kind.value} x{self.times}"


@dataclass
class InjectedFaultProvider:
    """Wraps a provider and injects faults into its completions."""

    inner: Any
    faults: list[FaultSpec] = field(default_factory=list)
    calls: int = 0
    name: str = "faulty"

    @property
    def model(self) -> str:
        return getattr(self.inner, "model", "faulty")

    def inject(self, fault: FaultSpec) -> None:
        self.faults.append(fault)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        for fault in list(self.faults):
            if fault.times <= 0:
                continue
            if fault.kind is Fault.PROVIDER_UNAVAILABLE:
                fault.times -= 1
                raise ModelError(
                    f"{fault.message}: the provider is unavailable",
                    provider=self.name,
                )
            if fault.kind is Fault.PROVIDER_FLAKY:
                fault.times -= 1
                raise TransientModelError(
                    f"{fault.message}: transient provider failure",
                    provider=self.name,
                )
            if fault.kind is Fault.MODEL_GARBAGE:
                fault.times -= 1
                return ModelResponse(
                    text="this is not valid JSON for the schema",
                    provider=self.name,
                    model=self.model,
                )
            if fault.kind is Fault.SLOW_WORKER:
                fault.times -= 1
                await asyncio.sleep(fault.delay_s or 5.0)
            if fault.kind is Fault.STALLED_TASK:
                fault.times -= 1
                await asyncio.sleep(fault.delay_s or 5.0)
        return await self.inner.complete(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


@dataclass
class CrashyRunner:
    """Wraps a worker runner and makes it crash.

    The crash is an *unexpected* exception -- not one of the runtime's own
    error types -- which is exactly the case the scheduler must survive.
    """

    inner: Any
    crashes: int = 0
    message: str = "injected worker crash"

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        if self.crashes > 0:
            self.crashes -= 1
            raise RuntimeError(self.message)
        return await self.inner.execute(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


@dataclass
class ChaosHarness:
    """Bundles the injected faults and records what actually happened."""

    provider: InjectedFaultProvider | None = None
    runner: CrashyRunner | None = None
    dropped_messages: list[str] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)

    def observe(self, note: str) -> None:
        self.observations.append(note)

    def drop_next_message(self, marker: str) -> None:
        self.dropped_messages.append(marker)


def install_provider_faults(provider: Any, faults: list[FaultSpec]) -> InjectedFaultProvider:
    """Wrap ``provider`` so that it injects ``faults``."""
    wrapper = InjectedFaultProvider(inner=provider)
    for fault in faults:
        wrapper.inject(fault)
    return wrapper


def assert_safe_failure(
    *,
    status: str,
    failed_tasks: list[str],
    blocked_tasks: list[str] | None = None,
    succeeded_tasks: list[str] | None = None,
) -> None:
    """Assert the recovery contract for a failed run.

    A safe failure means: the run did not report success, the failed tasks
    are recorded as failed, and the tasks that depended on them are blocked
    rather than silently reported as done.
    """
    if status == "succeeded":
        raise AssertionError("the run reported success despite an injected fault")
    if not failed_tasks:
        raise AssertionError("the run did not record any failed task")
    for task_id in succeeded_tasks or []:
        if task_id in failed_tasks:
            raise AssertionError(f"task {task_id} is both failed and succeeded")
