"""The run deadline: one absolute bound for a whole run.

A per-request timeout is not a whole-run deadline.  A run that makes two
hundred requests, each well inside its own limit, can still take longer than
anyone is willing to wait.  The deadline is therefore *absolute*: it covers
every phase of the run -- the model's download/load, repository inspection,
the initial plan, a bounded replan, the provider queue and request, the
worker and its tools, verification, integration and teardown -- and it
reserves time at the end for cleanup.

The deadline is enforced *cooperatively* but *comprehensively*: every phase
that can block asks the deadline how much budget is left before it starts,
clamps its own timeout to that budget, and checks the deadline again before
it creates any new work.  A phase that starts after the expiry therefore
fails fast instead of starting work it cannot finish, and no synchronous
operation can outlive the deadline merely because the scheduler has not
started yet.

Two mechanisms carry the deadline to the phases:

``Explicit injection``
    Every component that can block accepts an optional ``deadline``.  The
    composition root owns the deadline and hands it to the components it
    builds.  This is the wiring the orchestrator should use.

``The ambient run scope``
    :class:`RunScope` holds the deadline, the cancellation event and the
    test seam for the phase that is running.  It exists so that the phases
    which run *before* the scheduler starts -- inspection and planning -- are
    bounded by the same absolute deadline as the ones that run inside it,
    and so that a test can inject a blocking operation into a phase through
    the ordinary run entry point.

Out of scope: non-interruptible operating-system failures
---------------------------------------------------------

The deadline bounds the runtime's *cooperative* work.  It cannot bound the
kernel, and the following failures are explicitly **outside the supported
scope** of the deadline:

* a process in uninterruptible sleep (``D`` state) -- for example a read
  from a device or a network filesystem that has stopped responding;
* a kernel writeback stall, where dirty pages cannot be flushed;
* an uninterruptible ``fsync`` on a hung network filesystem (NFS, CIFS);
* a kernel deadlock or a wedged device driver.

These are not "slow operations" that a longer timeout would fix: such a
process cannot be interrupted, cannot be killed and cannot be cancelled, so
no amount of clamping, checking or polling inside the runtime can bound it.
A run stuck in one of them may exceed its deadline indefinitely.

The supported scope is therefore stated plainly: *the runtime never starts
work it cannot finish, and every operation it starts is interruptible by the
deadline or by cancellation -- unless the operating system has stopped
responding to the process itself.*  An operator who needs a hard guarantee
against a hung kernel must isolate the run at the process or machine
boundary (a supervised container that is killed as a unit, or a dedicated
host), because no in-process deadline can survive a kernel that does not
return from a syscall.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, TypeVar, runtime_checkable

from ..errors import AgentSquadError, ErrorClass
from ..errors import CancelledError as RunCancelledError
from ..store.events import EventLog, EventType

T = TypeVar("T")

CANCELLATION_ACK_S = 5.0
"""How long a phase may take to acknowledge a cancellation.

The run promises that a cancellation is acknowledged within five seconds.
A phase that suppresses ``CancelledError`` and keeps working would otherwise
make the run wait for it indefinitely, so the acknowledgement itself is
bounded: once the window passes the run stops waiting and reports the
deadline, whether or not the abandoned work has noticed.
"""

_SYNC_POLL_S = 0.05
"""How often a synchronous gate is polled for cancellation.

A synchronous phase cannot be interrupted, so its gate runs in a worker
thread.  The join is done in slices so that a cancellation is noticed
within one slice instead of after the phase's whole remaining budget.
"""


class DeadlineState(str, Enum):
    OK = "ok"
    APPROACHING = "approaching"
    EXPIRED = "expired"


class Phase(str, Enum):
    """The phases that one absolute deadline covers.

    The names are the vocabulary the run report and the event log use, so
    that "which phase ran out of time" is a recorded fact rather than an
    inference.
    """

    DOWNLOAD = "download"
    """Acquiring the model: the weights are fetched and loaded."""

    INSPECTION = "inspection"
    """Reading the repository to build the planning context."""

    PLANNING = "planning"
    """The initial plan."""

    REPLANNING = "replanning"
    """A bounded revision of the plan after a classified failure."""

    PROVIDER = "provider"
    """The provider queue wait and the model request itself."""

    WORKER = "worker"
    """A worker attempt and the tools it runs."""

    VERIFICATION = "verification"
    """The acceptance checks, per task and for the final candidate tree."""

    INTEGRATION = "integration"
    """Combining the produced patches and re-checking the combined tree."""

    TEARDOWN = "teardown"
    """Reclaiming what the run owns."""


class DeadlineExceeded(AgentSquadError):
    """The run ran out of its absolute deadline.

    It is an :class:`~agent_squad.errors.AgentSquadError` so that the
    failure taxonomy classifies it as a *timeout* rather than as an
    unknown error: a deadline expiry is neither a model failure nor a
    policy denial, and the run's outcome has to say which it was.
    """

    error_class = ErrorClass.TIMEOUT
    retryable = False

    def __init__(self, message: str, *, phase: str = "", **context: object) -> None:
        super().__init__(message, phase=phase, **context)
        self.phase = phase
        """The phase that observed the expiry, when it is known."""


@dataclass
class RunDeadline:
    """An absolute deadline for a whole run."""

    total_s: float | None = None
    reserve_s: float = 5.0
    started_at: float = 0.0
    observed_phase: str | None = None
    """The phase that observed the expiry.

    The first phase to notice that the budget is gone records it, so the
    report says which phase ran out of time rather than only that the run
    did.
    """
    _log: EventLog | None = None
    _run_id: str = ""
    _recorded: bool = False

    def start(self, now: float | None = None) -> None:
        self.started_at = now if now is not None else time.monotonic()
        # The deadline is put into the ambient scope as well as being handed
        # to the components explicitly: the phases that run before the
        # scheduler starts must be bounded by it too, and the composition
        # root is not the only thing that knows a run is in progress.
        bind_run_scope(deadline=self)

    # ------------------------------------------------------------- queries

    @property
    def enabled(self) -> bool:
        return self.total_s is not None and self.total_s > 0

    def elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        return time.monotonic() - self.started_at

    def remaining(self) -> float | None:
        """Seconds left, excluding the teardown reserve."""
        total = self.total_s
        if not self.enabled or total is None:
            return None
        return max(0.0, total - self.reserve_s - self.elapsed())

    def teardown_remaining(self) -> float | None:
        """Seconds left *including* the teardown reserve.

        Teardown is the one phase that is allowed to spend the reserve: its
        job is to reclaim what the run owns, and a run that gives up on
        cleanup because the budget is gone has left the machine dirty.
        """
        total = self.total_s
        if not self.enabled or total is None:
            return None
        return max(0.0, total - self.elapsed())

    def state(self) -> DeadlineState:
        if not self.enabled:
            return DeadlineState.OK
        remaining = self.remaining() or 0.0
        if remaining <= 0:
            return DeadlineState.EXPIRED
        if remaining < max(1.0, 0.1 * (self.total_s or 1.0)):
            return DeadlineState.APPROACHING
        return DeadlineState.OK

    def expired(self) -> bool:
        return self.state() is DeadlineState.EXPIRED

    def clamp(self, requested: float | None) -> float | None:
        """Reduce a timeout so it cannot outlive the deadline."""
        if requested is None:
            remaining = self.remaining()
            return remaining
        remaining = self.remaining()
        if remaining is None:
            return requested
        return max(0.1, min(requested, remaining))

    # ------------------------------------------------------------- records

    def observe(self, phase: str) -> None:
        """Record the phase that observed the expiry.

        Only the first observation is kept: the innermost phase that
        noticed the expiry is the one that was actually running, and the
        phases around it were interrupted by it.
        """
        if phase and not self.observed_phase:
            self.observed_phase = phase
        self.record()

    def attach(self, log: EventLog | None, run_id: str = "") -> None:
        """Give the deadline an event log so that it can record its state.

        The deadline records its own summary as soon as it observes the
        expiry, so that a run which stops in a phase that has no log of its
        own still leaves a durable record of why it stopped.
        """
        if log is None:
            return
        self._log = log
        self._run_id = run_id or self._run_id
        if self.expired():
            self.record()

    def record(self, log: EventLog | None = None, run_id: str = "") -> None:
        """Write the deadline's summary to the event log, once.

        The summary states the deadline's state, how long the run had been
        going, how much budget was left and which phase observed the
        expiry, so that the record says *why* the run stopped rather than
        only that it did.
        """
        if log is not None:
            self._log = log
        if run_id:
            self._run_id = run_id
        if self._log is None or self._recorded:
            return
        self._recorded = True
        self._log.append(
            EventType.RUN_FAILED,
            run_id=self._run_id,
            payload={
                "reason": "deadline_exceeded",
                "error_class": ErrorClass.TIMEOUT.value,
                **self.summary(),
            },
        )

    def check(self, phase: str = "") -> None:
        """Raise when the deadline has passed.

        This is the fail-fast guard: a phase that starts after the expiry
        must not create work it cannot finish.
        """
        if self.expired():
            self.observe(phase)
            raise DeadlineExceeded(
                "the run exceeded its absolute deadline"
                + (f" before {phase}" if phase else "")
                + f": {self.summary()}",
                phase=phase,
            )

    def summary(self) -> dict[str, object]:
        # Narrow the optional once, so the rounded value is a float rather
        # than ``float | None`` inside the conditional expression.
        remaining = self.remaining()
        return {
            "total_s": self.total_s,
            "reserve_s": self.reserve_s,
            "elapsed_s": round(self.elapsed(), 3),
            "remaining_s": None if remaining is None else round(remaining, 3),
            "state": self.state().value,
            "phase": self.observed_phase,
        }


class DeadlineGuard:
    """Checks the deadline before each phase and clamps timeouts."""

    def __init__(self, deadline: RunDeadline, *, phase: str = "") -> None:
        self._deadline = deadline
        self._phase = phase

    @property
    def deadline(self) -> RunDeadline:
        return self._deadline

    def check(self, phase: str | None = None) -> None:
        """Raise if the deadline has passed."""
        name = phase if phase is not None else self._phase
        self._deadline.check(name)

    def clamp(self, requested: float | None) -> float | None:
        return self._deadline.clamp(requested)


# --------------------------------------------------------------------- scope


@runtime_checkable
class PhaseGate(Protocol):
    """The seam through which a test injects a blocking operation.

    Every deadline-aware phase announces itself through the gate before it
    starts its blocking work.  A gate may block there: the phase's work is
    then bounded by the run's deadline, so the run still finishes on time
    and the test can prove that the phase really was entered.

    The two methods exist because the phases are not all the same shape.
    An asynchronous phase is interrupted by cancellation, so its gate may
    await.  A synchronous phase cannot be interrupted, so its gate is
    called from a worker thread and the runtime joins it with the remaining
    budget -- a gate that never returns still cannot keep the run past its
    deadline.
    """

    async def enter(self, phase: str) -> None:
        """Announce the start of an asynchronous phase; may block."""
        ...

    def enter_sync(self, phase: str) -> None:
        """Announce the start of a synchronous phase; may block."""
        ...


@dataclass
class RunScope:
    """The ambient context every phase of one run executes in.

    The scope is what makes the deadline reach the phases that the
    composition root does not build itself -- inspection and planning run
    before the scheduler exists, and the final judgement runs after it has
    finished -- and what lets a test reach *any* phase through the ordinary
    run entry point.

    The scope is shared by the phases of one run.  It is deliberately
    mutable: the composition root binds the deadline and the cancellation
    event to it once, and every phase reads them.
    """

    deadline: RunDeadline | None = None
    cancellation: asyncio.Event | None = None
    gate: PhaseGate | None = None
    log: EventLog | None = None
    """The run's event log, so that a phase without a log of its own can
    still leave a durable record."""
    run_id: str = ""
    plan_calls: int = 0
    """How many plans this run has asked for.

    The first plan of a run is the initial plan; any later plan is a
    bounded revision of it.  The count is what tells the planner which of
    the two phases it is running in.
    """

    async def enter(self, phase: str) -> None:
        """Announce an asynchronous phase to the gate."""
        if self.gate is not None:
            await self.gate.enter(phase)

    def enter_sync(self, phase: str) -> None:
        """Announce a synchronous phase to the gate.

        The gate is called directly: a caller that has no deadline has no
        way to bound a blocking gate, and pretending otherwise would hide
        the fact that the phase is unbounded.
        """
        if self.gate is not None:
            self.gate.enter_sync(phase)

    def block_sync(
        self,
        phase: str,
        *,
        deadline: RunDeadline | None = None,
        cancellation: asyncio.Event | None = None,
    ) -> None:
        """Announce a synchronous phase, bounding a blocking gate.

        The gate runs in a daemon thread which is joined with the run's
        remaining budget.  A gate that blocks forever therefore cannot
        keep the run past its deadline, which is the property the deadline
        promises for the phases that cannot be interrupted.

        The join is done in slices so that a cancellation is acknowledged
        promptly rather than after the phase's whole remaining budget: the
        thread cannot be interrupted, but the *run* does not have to keep
        waiting for it.  ``cancellation`` is accepted explicitly because a
        caller that owns the run's token (the scheduler) must be able to
        pass it even when the ambient scope was not given one.
        """
        if self.gate is None:
            return
        if deadline is None:
            deadline = self.deadline
        budget = deadline.remaining() if deadline is not None and deadline.enabled else None
        if budget is None:
            self.gate.enter_sync(phase)
            return
        thread = threading.Thread(
            target=self.gate.enter_sync,
            args=(phase,),
            name=f"squad-phase-{phase}",
            daemon=True,
        )
        thread.start()
        end = time.monotonic() + max(0.0, budget)
        token = cancellation if cancellation is not None else self.cancellation
        while thread.is_alive():
            if token is not None and token.is_set():
                return
            left = end - time.monotonic()
            if left <= 0:
                return
            thread.join(timeout=min(_SYNC_POLL_S, left))


_SCOPE: ContextVar[RunScope | None] = ContextVar("agent_squad_run_scope", default=None)


def current_run_scope() -> RunScope:
    """The scope of the run that is executing, if there is one."""
    scope = _SCOPE.get()
    if scope is None:
        scope = RunScope()
        _SCOPE.set(scope)
    return scope


@contextlib.contextmanager
def run_scope(
    *,
    deadline: RunDeadline | None = None,
    cancellation: asyncio.Event | None = None,
    gate: PhaseGate | None = None,
    log: EventLog | None = None,
    run_id: str = "",
) -> Iterator[RunScope]:
    """Install a run scope for the duration of the block.

    This is how a caller (a test, or a benchmark) reaches every phase of a
    run without the composition root having to know about it.
    """
    scope = RunScope(
        deadline=deadline,
        cancellation=cancellation,
        gate=gate,
        log=log,
        run_id=run_id,
    )
    token = _SCOPE.set(scope)
    try:
        yield scope
    finally:
        _SCOPE.reset(token)


def bind_run_scope(
    *,
    deadline: RunDeadline | None = None,
    cancellation: asyncio.Event | None = None,
    log: EventLog | None = None,
    run_id: str = "",
) -> RunScope:
    """Attach the run's deadline, cancellation and log to the ambient scope.

    The scope that is already installed is updated rather than replaced,
    so a caller that installed a gate keeps it.
    """
    scope = current_run_scope()
    if deadline is not None:
        scope.deadline = deadline
    if cancellation is not None:
        scope.cancellation = cancellation
    if log is not None:
        scope.log = log
    if run_id:
        scope.run_id = run_id
    if scope.deadline is not None:
        scope.deadline.attach(scope.log, scope.run_id)
    return scope


def unbind_run_scope() -> None:
    """Detach the deadline and the cancellation from the ambient scope.

    A finished run must not leave an expired deadline behind for the next
    piece of code that happens to run in the same process.
    """
    scope = _SCOPE.get()
    if scope is not None:
        scope.deadline = None
        scope.cancellation = None


# --------------------------------------------------------------------- phase


def _budget(
    deadline: RunDeadline | None, timeout_s: float | None
) -> tuple[float | None, str | None]:
    """The time a phase may run, and what ended it.

    The deadline wins whenever it is the tighter bound: the run's absolute
    limit is never extended by a phase's own timeout.
    """
    remaining = deadline.remaining() if deadline is not None and deadline.enabled else None
    if remaining is None:
        return (timeout_s, "timeout") if timeout_s is not None else (None, None)
    if timeout_s is None:
        return (remaining, "deadline")
    if timeout_s <= remaining:
        return (timeout_s, "timeout")
    return (remaining, "deadline")


def _consume_result(task: asyncio.Task[Any]) -> None:
    """Retrieve an abandoned phase's result so its failure is not a warning."""
    with contextlib.suppress(BaseException):
        task.exception()


async def run_phase(
    work: Callable[[], Awaitable[T]],
    *,
    phase: str,
    deadline: RunDeadline | None = None,
    cancellation: asyncio.Event | None = None,
    timeout_s: float | None = None,
) -> T:
    """Run one phase's blocking work under the run's deadline.

    The phase is bounded three ways at once:

    * it refuses to start once the deadline has passed, so no work is
      created that cannot be finished;
    * the work is interrupted when the deadline expires, and the phase
      reports which phase ran out of time;
    * the work is interrupted when the run is cancelled, and the phase
      reports the cancellation.

    ``work`` is a factory rather than an awaitable so that the work is
    only started once the phase has been admitted: a phase that is refused
    must not have started anything at all.

    The cancellation acknowledgement is itself bounded by
    :data:`CANCELLATION_ACK_S`: a phase that swallows ``CancelledError``
    cannot make the run wait past the promised window.
    """
    scope = current_run_scope()
    if deadline is None:
        deadline = scope.deadline
    if cancellation is None:
        cancellation = scope.cancellation

    if deadline is not None:
        deadline.check(phase)

    async def body() -> T:
        # The seam is inside the bounded region on purpose: a test that
        # blocks here is interrupted by the deadline exactly like a slow
        # provider request would be.
        await scope.enter(phase)
        return await work()

    task: asyncio.Task[T] = asyncio.ensure_future(body())
    budget, cause = _budget(deadline, timeout_s)
    timer: asyncio.Future[Any] | None = None
    cancel_waiter: asyncio.Future[Any] | None = None
    waiters: list[asyncio.Future[Any]] = []
    if budget is not None:
        timer = asyncio.ensure_future(asyncio.sleep(budget))
        waiters.append(timer)
    if cancellation is not None:
        cancel_waiter = asyncio.ensure_future(cancellation.wait())
        waiters.append(cancel_waiter)

    try:
        if not waiters:
            return await task
        done, _pending = await asyncio.wait(
            {task, *waiters}, return_when=asyncio.FIRST_COMPLETED
        )
        if task in done:
            # The phase finished on its own.  If it failed because the
            # deadline expired, that failure carries the phase that
            # observed it and is re-raised unchanged.
            return task.result()
        task.cancel()
        # A distinct name: this wait observes a different set than the
        # first one, and reusing the name would widen the inferred type.
        acknowledged, _ack_pending = await asyncio.wait(
            {task}, timeout=CANCELLATION_ACK_S
        )
        inner: BaseException | None = None
        if task in acknowledged:
            results = await asyncio.gather(task, return_exceptions=True)
            inner = results[0] if results and isinstance(results[0], BaseException) else None
        else:
            # The phase did not acknowledge the cancellation within the
            # window.  The run stops waiting anyway -- the bound is the
            # promise, and an uncooperative coroutine cannot extend it --
            # while consuming the eventual result so the abandoned task
            # does not become an unhandled-exception warning.
            task.add_done_callback(_consume_result)
        if isinstance(inner, DeadlineExceeded):
            raise inner
        if cancel_waiter is not None and cancel_waiter in done:
            raise RunCancelledError(
                f"the {phase} phase was cancelled", phase=phase
            )
        if cause == "deadline" and deadline is not None:
            deadline.observe(phase)
            raise DeadlineExceeded(
                f"the {phase} phase exceeded the run's deadline: {deadline.summary()}",
                phase=phase,
            )
        raise TimeoutError(f"the {phase} phase exceeded its {timeout_s}s timeout")
    except asyncio.CancelledError:
        # The region was cancelled from outside.  If the deadline has
        # passed, that is the reason; otherwise the cancellation is
        # propagated unchanged.
        if deadline is not None and deadline.expired():
            deadline.observe(phase)
            raise DeadlineExceeded(
                f"the {phase} phase exceeded the run's deadline: {deadline.summary()}",
                phase=phase,
            ) from None
        raise
    finally:
        for waiter in waiters:
            waiter.cancel()


@contextlib.contextmanager
def teardown_window(
    deadline: RunDeadline | None = None,
    *,
    phase: str = Phase.TEARDOWN.value,
    gate: PhaseGate | None = None,
) -> Iterator[None]:
    """Bound the teardown phase.

    Teardown is the one phase that may spend the teardown reserve: its job
    is to reclaim what the run owns, and a run that abandons its cleanup
    because the budget is gone leaves the machine dirty.  The window
    announces the phase to the gate so that a test can block it, and it
    refuses to let teardown start once the *whole* budget is gone.
    """
    if deadline is None:
        deadline = current_run_scope().deadline
    scope = current_run_scope()
    if deadline is not None:
        budget = deadline.teardown_remaining()
        if budget is not None and budget <= 0:
            deadline.observe(phase)
            raise DeadlineExceeded(
                f"the {phase} phase could not start: the run's whole budget is gone",
                phase=phase,
            )
    if gate is not None:
        scope.block_sync(phase, deadline=deadline)
    else:
        scope.enter_sync(phase)
    yield


# ------------------------------------------------------------------ records


def record_deadline_summary(
    log: EventLog | None,
    deadline: RunDeadline | None,
    *,
    run_id: str = "",
) -> None:
    """Record the deadline's state in the run's event log.

    The summary states the deadline's state, how long the run had been
    going, how much budget was left and which phase observed the expiry,
    so that the record says *why* the run stopped rather than only that it
    did.  The record is written once: the deadline remembers that it has
    already reported itself.
    """
    if deadline is None:
        return
    deadline.record(log, run_id)


__all__ = [
    "CANCELLATION_ACK_S",
    "DeadlineExceeded",
    "DeadlineGuard",
    "DeadlineState",
    "Phase",
    "PhaseGate",
    "RunDeadline",
    "RunScope",
    "bind_run_scope",
    "current_run_scope",
    "record_deadline_summary",
    "run_phase",
    "run_scope",
    "teardown_window",
    "unbind_run_scope",
]
