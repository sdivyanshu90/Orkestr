"""The run deadline.

A per-request timeout is not a whole-run deadline.  A run that makes two
hundred requests, each well inside its own limit, can still take longer than
anyone is willing to wait.  The deadline is therefore *absolute*: it covers
inspection, planning, every worker request, verification, retries and
integration, and it reserves time at the end for cleanup.

The deadline is enforced cooperatively: the phases check it, and the
scheduler's per-attempt timeout is clamped to whatever remains.  That means a
run that is already over budget fails *fast* rather than starting another
attempt it cannot finish.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum


class DeadlineState(str, Enum):
    OK = "ok"
    APPROACHING = "approaching"
    EXPIRED = "expired"


@dataclass
class RunDeadline:
    """An absolute deadline for a whole run."""

    total_s: float | None = None
    reserve_s: float = 5.0
    started_at: float = 0.0

    def start(self, now: float | None = None) -> None:
        self.started_at = now if now is not None else time.monotonic()

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
        if not self.enabled:
            return None
        return max(0.0, self.total_s - self.reserve_s - self.elapsed())

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

    def summary(self) -> dict[str, object]:
        return {
            "total_s": self.total_s,
            "reserve_s": self.reserve_s,
            "elapsed_s": round(self.elapsed(), 3),
            "remaining_s": None if self.remaining() is None else round(self.remaining(), 3),
            "state": self.state().value,
        }


class DeadlineExceeded(Exception):
    """The run ran out of its absolute deadline."""


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
        if self._deadline.expired():
            raise DeadlineExceeded(
                "the run exceeded its absolute deadline"
                + (f" before {phase}" if phase else "")
                + f": {self._deadline.summary()}"
            )

    def clamp(self, requested: float | None) -> float | None:
        return self._deadline.clamp(requested)
