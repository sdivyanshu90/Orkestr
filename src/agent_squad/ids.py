"""Lexicographically sortable identifiers.

Identifiers are timestamp-prefixed so that lexical ordering matches
chronological ordering.  This makes the append-only event log trivially
sortable without an extra index, and keeps debugging sessions readable
(``tsk_01H...`` sorts before ``tsk_01J...``).

The format is Crockford base32, which excludes characters that are easily
confused (``I``, ``L``, ``O``, ``U``) and is URL/filename safe.
"""

from __future__ import annotations

import os
import threading
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_lock = threading.Lock()
_last_ms = 0
_counter = 0


def _encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def new_id(prefix: str) -> str:
    """Return a fresh sortable identifier such as ``tsk_01JQ8Z...``.

    Within the same millisecond a process-local counter is used to keep the
    identifiers strictly increasing.  The counter is reset when the
    millisecond changes.
    """
    global _last_ms, _counter
    with _lock:
        now_ms = int(time.time() * 1000)
        if now_ms == _last_ms:
            _counter += 1
            if _counter >= 1024:
                # Borrow from the next millisecond rather than wrapping, so
                # monotonicity is never violated.
                now_ms = _last_ms + 1
                _last_ms = now_ms
                _counter = 0
        else:
            _last_ms = now_ms
            _counter = 0
        counter = _counter
    rand = int.from_bytes(os.urandom(5), "big")
    body = _encode(now_ms, 10) + _encode((counter & 0x3FF) << 20 | (rand & 0xFFFFF), 5)
    return f"{prefix}_{body}"


def run_id() -> str:
    return new_id("run")


def task_id() -> str:
    return new_id("tsk")


def message_id() -> str:
    return new_id("msg")


def artifact_id() -> str:
    return new_id("art")


def worktree_id() -> str:
    return new_id("wt")


def span_id() -> str:
    return new_id("spn")
