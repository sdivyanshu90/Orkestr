"""Budget accounting.

The budget manager is the enforcement point for every monetary and token
ceiling in the system.  It is consulted *before* a model call is issued (a
pre-flight check against the remaining allowance) and updated *after* the
call with the usage the provider reported.

The separation matters: pre-flight uses the last known remaining allowance,
so a call that would exceed the ceiling is never started; the post-flight
update uses reported usage, so accounting is based on measurement rather than
on a guess made before the call.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..config import BudgetConfig, ProviderConfig
from ..errors import BudgetExceeded
from .base import Usage
from .registry import compute_cost


@dataclass
class BudgetSnapshot:
    tokens_used: int = 0
    cost_used_usd: float = 0.0
    token_limit: int | None = None
    cost_limit_usd: float | None = None
    calls: int = 0

    @property
    def tokens_remaining(self) -> int | None:
        if self.token_limit is None:
            return None
        return max(0, self.token_limit - self.tokens_used)

    @property
    def cost_remaining_usd(self) -> float | None:
        if self.cost_limit_usd is None:
            return None
        return max(0.0, round(self.cost_limit_usd - self.cost_used_usd, 10))

    def to_dict(self) -> dict[str, object]:
        return {
            "tokens_used": self.tokens_used,
            "cost_used_usd": self.cost_used_usd,
            "token_limit": self.token_limit,
            "cost_limit_usd": self.cost_limit_usd,
            "tokens_remaining": self.tokens_remaining,
            "cost_remaining_usd": self.cost_remaining_usd,
            "calls": self.calls,
        }


@dataclass
class ScopeBudget:
    """A budget bucket keyed by an arbitrary scope string."""

    key: str
    token_limit: int | None = None
    cost_limit_usd: float | None = None
    tokens_used: int = 0
    cost_used_usd: float = 0.0
    calls: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "tokens_used": self.tokens_used,
            "cost_used_usd": self.cost_used_usd,
            "token_limit": self.token_limit,
            "cost_limit_usd": self.cost_limit_usd,
        }


class BudgetManager:
    """Tracks and enforces token and cost ceilings at several scopes."""

    def __init__(self, config: BudgetConfig, provider_config: ProviderConfig) -> None:
        self._config = config
        self._provider_config = provider_config
        # Re-entrant: `record` and `check` call `scope` while holding the lock.
        self._lock = threading.RLock()
        self._scopes: dict[str, ScopeBudget] = {}

    # ------------------------------------------------------------- scopes

    def scope(self, key: str) -> ScopeBudget:
        with self._lock:
            bucket = self._scopes.get(key)
            if bucket is None:
                bucket = ScopeBudget(key=key)
                self._scopes[key] = bucket
            return bucket

    def set_scope_limits(
        self,
        key: str,
        *,
        token_limit: int | None = None,
        cost_limit_usd: float | None = None,
    ) -> ScopeBudget:
        with self._lock:
            bucket = self.scope(key)
            if token_limit is not None:
                bucket.token_limit = token_limit
            if cost_limit_usd is not None:
                bucket.cost_limit_usd = cost_limit_usd
            return bucket

    # ---------------------------------------------------------- enforcement

    def check(self, key: str, *, extra_tokens: int = 0, extra_cost_usd: float = 0.0) -> None:
        """Raise :class:`BudgetExceeded` if the scope may not proceed.

        ``extra_tokens``/``extra_cost_usd`` describe the *expected* cost of
        the call about to be made, so that a call which would cross the
        ceiling is refused rather than started and then aborted.
        """
        with self._lock:
            bucket = self.scope(key)
            if bucket.token_limit is not None:
                projected = bucket.tokens_used + extra_tokens
                if projected > bucket.token_limit:
                    raise BudgetExceeded(
                        f"token budget exhausted for {key!r}: "
                        f"{projected} > {bucket.token_limit}",
                        scope=key,
                        projected_tokens=projected,
                        limit=bucket.token_limit,
                    )
            if bucket.cost_limit_usd is not None:
                projected = bucket.cost_used_usd + extra_cost_usd
                if projected > bucket.cost_limit_usd:
                    raise BudgetExceeded(
                        f"cost budget exhausted for {key!r}: "
                        f"{projected:.6f} > {bucket.cost_limit_usd:.6f}",
                        scope=key,
                        projected_cost=projected,
                        limit=bucket.cost_limit_usd,
                    )

    def record(
        self,
        key: str,
        usage: Usage,
        *,
        provider_config: ProviderConfig | None = None,
    ) -> float:
        """Record usage against a scope and return the cost that was booked."""
        cfg = provider_config or self._provider_config
        cost = compute_cost(usage, cfg)
        with self._lock:
            bucket = self.scope(key)
            bucket.tokens_used += usage.total_tokens
            bucket.cost_used_usd = round(bucket.cost_used_usd + cost, 10)
            bucket.calls += 1
        return cost

    # ------------------------------------------------------------ snapshots

    def snapshot(self, key: str = "run") -> BudgetSnapshot:
        with self._lock:
            bucket = self.scope(key)
            return BudgetSnapshot(
                tokens_used=bucket.tokens_used,
                cost_used_usd=bucket.cost_used_usd,
                token_limit=bucket.token_limit,
                cost_limit_usd=bucket.cost_limit_usd,
                calls=bucket.calls,
            )

    def all_scopes(self) -> list[dict[str, object]]:
        with self._lock:
            return [b.to_dict() for b in sorted(self._scopes.values(), key=lambda b: b.key)]

    def total_tokens(self) -> int:
        with self._lock:
            return sum(b.tokens_used for b in self._scopes.values())

    def total_cost_usd(self) -> float:
        with self._lock:
            return round(sum(b.cost_used_usd for b in self._scopes.values()), 10)
