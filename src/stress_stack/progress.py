"""Live progress for long stages, written to stderr.

A run takes minutes and printed nothing until it finished. Progress goes to
stderr so stdout stays parseable, and the default reporter is silent so the
library and the tests behave exactly as before.
"""

from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import IO, Iterator

_TICK = "✓"
_CROSS = "✗"
_DOT = "·"
_ARROW = "›"


class Reporter:
    """Silent by default. Every hook is a no-op unless a Console replaces it."""

    def stage_start(self, name: str, index: int = 0, total: int = 0) -> None: ...

    def stage_end(self, name: str, status: str, seconds: float, detail: str = "") -> None: ...

    def step(self, message: str, current: int = 0, total: int = 0) -> None: ...

    def note(self, message: str) -> None: ...


@dataclass
class Console(Reporter):
    """Prints as work happens, one line per stage plus a live step line."""

    stream: IO[str] = field(default_factory=lambda: sys.stderr)
    started: float = field(default_factory=time.monotonic)
    # Named up front when a single stage runs on its own; `run` overwrites it
    # per stage, so a step line always says which stage it belongs to.
    stage: str = ""
    _open_line: bool = False

    @property
    def tty(self) -> bool:
        """Interactive? A closed or detached stream answers no rather than raising."""
        try:
            return bool(getattr(self.stream, "isatty", lambda: False)())
        except (OSError, ValueError):
            return False

    def stage_start(self, name: str, index: int = 0, total: int = 0) -> None:
        self.stage = name
        position = f"[{index}/{total}] " if total else ""
        self._write(f"{_ARROW} {position}{name}", newline=not self.tty)
        self._open_line = self.tty

    def stage_end(self, name: str, status: str, seconds: float, detail: str = "") -> None:
        mark = _TICK if status in {"ok", "skipped"} else (_DOT if status == "degraded" else _CROSS)
        self._clear()
        suffix = f"  {detail}" if detail else ""
        self._write(f"{mark} {name:<11}{status:<9}{seconds:>7.1f}s{suffix}", newline=True)
        self.stage = ""

    def step(self, message: str, current: int = 0, total: int = 0) -> None:
        """One unit of work inside a stage. Overwritten in place on a terminal."""
        counter = f"{current}/{total} " if total else ""
        label = f"{self.stage} " if self.stage else ""
        line = f"  {_DOT} {label}{counter}{message}"
        if not self.tty:
            self._write(line, newline=True)
            return
        self._clear()
        self._write(line[: _width()], newline=False)
        self._open_line = True

    def note(self, message: str) -> None:
        self._clear()
        self._write(f"  {message}", newline=True)

    def _clear(self) -> None:
        if self._open_line and self.tty:
            self._write("\r\033[K", newline=False)
        self._open_line = False

    def _write(self, text: str, *, newline: bool) -> None:
        try:
            self.stream.write(text + ("\n" if newline else ""))
            self.stream.flush()
        except (OSError, ValueError):
            # A closed or redirected stream must never take a run down with it.
            pass


def _width(default: int = 100) -> int:
    try:
        import shutil

        return max(40, shutil.get_terminal_size((default, 24)).columns - 1)
    except Exception:  # noqa: BLE001 — width is cosmetic
        return default


_active: Reporter = Reporter()


def reporter() -> Reporter:
    """The installed reporter, or the silent one."""
    return _active


@contextmanager
def reporting(target: Reporter) -> Iterator[Reporter]:
    """Install a reporter for the duration of a run, then restore the previous."""
    global _active
    previous = _active
    _active = target
    try:
        yield target
    finally:
        _active = previous
