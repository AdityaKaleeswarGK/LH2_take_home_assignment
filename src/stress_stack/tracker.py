"""Shared completion state and dependency synchronization tracker.

Adapted from AlphaStack's FileTracker with Condition-based thread notification,
cycle detection, and deadlock breaking.

Allows concurrent benchmark verification jobs to wait for prerequisites (e.g.
container image builds or coverage maps) without polling.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from threading import Condition, Lock
from typing import Any

logger = logging.getLogger(__name__)


class TaskTracker:
    """Thread-safe task completion and prerequisite synchronization tracker."""

    def __init__(self) -> None:
        self._condition = Condition(Lock())
        self._done: set[str] = set()
        self._failed: set[str] = set()
        self._waiting_on: dict[str, str] = {}
        self._results: dict[str, Any] = {}

    def mark_done(self, task_id: str, result: Any = None) -> None:
        with self._condition:
            self._done.add(task_id)
            if result is not None:
                self._results[task_id] = result
            self._condition.notify_all()

    def mark_failed(self, task_id: str, error: str = "") -> None:
        with self._condition:
            self._failed.add(task_id)
            self._results[task_id] = {"error": error}
            self._condition.notify_all()

    def _creates_cycle(self, waiter: str, target: str) -> bool:
        seen: set[str] = set()
        current: str | None = target
        while current is not None and current not in seen:
            if current == waiter:
                return True
            seen.add(current)
            current = self._waiting_on.get(current)
        return False

    def wait_for(
        self, target_id: str, waiter_id: str | None = None, timeout: float | None = 60.0
    ) -> str:
        """Block until target_id is done or failed. Returns 'done', 'failed', 'timeout', or 'deadlock'."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            if waiter_id is not None:
                if self._creates_cycle(waiter_id, target_id):
                    return "deadlock"
                self._waiting_on[waiter_id] = target_id
            try:
                while target_id not in self._done and target_id not in self._failed:
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        return "timeout"
                    self._condition.wait(timeout=remaining)
                return "done" if target_id in self._done else "failed"
            finally:
                if waiter_id is not None:
                    self._waiting_on.pop(waiter_id, None)

    def get_result(self, task_id: str) -> Any:
        with self._condition:
            return self._results.get(task_id)

    def is_done(self, task_id: str) -> bool:
        with self._condition:
            return task_id in self._done
