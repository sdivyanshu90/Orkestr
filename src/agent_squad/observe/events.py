"""Structured event emission.

Two output channels exist and they have different audiences:

* **Human-readable logs** go to stderr through ``rich`` (or plain JSON when
  ``log_json`` is set), for operators watching a run.
* **Machine-readable events** are appended to the run's event log, for
  tooling, replay and benchmarking.

The two are deliberately separate: a log line is a diagnostic, an event is a
fact about the run.  Nothing in the control plane reads log lines.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, TextIO

from ..models.task import utcnow


@dataclass
class StructuredLogger:
    """A minimal structured logger with a pluggable sink."""

    level: str = "INFO"
    json_output: bool = False
    stream: TextIO = field(default_factory=lambda: sys.stderr)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _context: dict[str, Any] = field(default_factory=dict)

    LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}

    def bind(self, **context: Any) -> StructuredLogger:
        child = StructuredLogger(
            level=self.level, json_output=self.json_output, stream=self.stream
        )
        child._context = {**self._context, **context}
        return child

    def _emit(self, level: str, message: str, **fields: Any) -> None:
        if self.LEVELS.get(level, 20) < self.LEVELS.get(self.level, 20):
            return
        payload = {
            "ts": utcnow().isoformat(),
            "level": level,
            "message": message,
            **self._context,
            **fields,
        }
        with self._lock:
            if self.json_output:
                self.stream.write(json.dumps(payload, default=str) + "\n")
            else:
                extras = " ".join(
                    f"{k}={v}" for k, v in payload.items() if k not in ("ts", "level", "message")
                )
                self.stream.write(f"[{level}] {message} {extras}\n".rstrip() + "\n")
            self.stream.flush()

    def debug(self, message: str, **fields: Any) -> None:
        self._emit("DEBUG", message, **fields)

    def info(self, message: str, **fields: Any) -> None:
        self._emit("INFO", message, **fields)

    def warning(self, message: str, **fields: Any) -> None:
        self._emit("WARNING", message, **fields)

    def error(self, message: str, **fields: Any) -> None:
        self._emit("ERROR", message, **fields)

    def exception(self, message: str, exc: BaseException, **fields: Any) -> None:
        self._emit(
            "ERROR",
            message,
            error=str(exc),
            error_type=type(exc).__name__,
            **fields,
        )


@dataclass
class Timer:
    """A small monotonic timer used to measure phase durations."""

    label: str
    started: float = field(default_factory=time.monotonic)

    def stop(self) -> float:
        return time.monotonic() - self.started
