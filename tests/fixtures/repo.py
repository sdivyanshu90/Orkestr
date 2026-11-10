"""Test fixtures.

The canonical fixture definitions live in
:mod:`agent_squad.benchmark.repo_fixture` so that the benchmark harness and
the test-suite use exactly the same repositories and requests.
"""

from __future__ import annotations

from agent_squad.benchmark.repo_fixture import (
    BASIC_TEST,
    CALC_SOURCE,
    ROOT_CONFTEST,
    ROUNDING_SOURCE,
    ROUNDING_TEST,
    conflicting_spec,
    git,
    init_repo,
    sample_spec,
)

__all__ = [
    "BASIC_TEST",
    "CALC_SOURCE",
    "ROOT_CONFTEST",
    "ROUNDING_SOURCE",
    "ROUNDING_TEST",
    "conflicting_spec",
    "git",
    "init_repo",
    "sample_spec",
]
