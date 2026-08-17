"""A read-only tool surface an agent may use to explore one task.

This is deliberately not a shell. Three properties are worth more here than
generality:

* **The tree is the task's ``input/``, not the repository.** A shell rooted at
  the clone can run ``git log -p`` and read the very commit that fixes the task.
  An agent that justified a difficulty label after reading the answer would be
  describing the solution, and that justification ships in ``task.json``. The
  history simply is not reachable from here.
* **Every path is resolved and re-checked against the root.** ``../`` and
  symlinks are how a scoped reader stops being scoped.
* **Nothing observes changing state.** The tools are pure functions of a fixed
  tree and a fixed index, which is what lets a cached conversation replay turn
  for turn: the same question asked twice returns the same bytes.

The structural queries are served from the index the pipeline already built, so
the agent inherits the call graph and the coverage map rather than re-deriving
them by grepping.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# A tool result is not paid for once. Every later turn re-sends the whole
# conversation, so one 8000-character file read rides along in each of the turns
# that follow it — measured on glom, the median request reached 32k characters
# and the largest 98k, and the wall clock is set by the slowest task's last turn.
# So reads are windowed by default and the caller asks for more if it needs more.
_FILE_BUDGET = 4000
_WINDOW_LINES = 120
_MATCH_LIMIT = 40
_ENTRY_LIMIT = 200

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a window of a UTF-8 text file from the task's pre-change tree. "
                f"Returns {_WINDOW_LINES} lines from 'start_line' (default 1) and "
                "reports the total line count, so ask again with a later start_line "
                "to keep reading. Use grep first to find the line you want."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path"],
                "properties": {
                    "path": {"type": "string", "description": "Repository-relative path."},
                    "start_line": {
                        "type": "integer",
                        "description": "1-indexed first line to return. Defaults to 1.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": (
                "Search the pre-change tree for a Python regular expression and return "
                "matching lines with their paths and line numbers."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["pattern"],
                "properties": {
                    "pattern": {"type": "string"},
                    "glob": {
                        "type": "string",
                        "description": "Optional filename filter, e.g. '*.py'.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List the entries of a directory in the pre-change tree.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path"],
                "properties": {"path": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "callers_of",
            "description": (
                "Who references a symbol, from the repository graph. Takes a symbol id "
                "such as 'glom/core.py::Path.from_t'."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["symbol_id"],
                "properties": {"symbol_id": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tests_for_module",
            "description": (
                "Which tests execute a module, from the measured coverage map. Takes a "
                "dotted module name such as 'glom.core'."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["module"],
                "properties": {"module": {"type": "string"}},
            },
        },
    },
]

TOOL_NAMES = frozenset(
    entry["function"]["name"] for entry in TOOLS  # type: ignore[index]
)


class ScopeError(Exception):
    """A path that resolved outside the tree the agent is allowed to read."""


@dataclass
class Explorer:
    """Serves the tool calls for exactly one task.

    ``calls`` records what was asked and whether it succeeded. It ships with the
    task so a reader can see what the agent actually looked at before it formed
    a judgement — a claim about reasoning is worth little without it.
    """

    tree: Path
    index: sqlite3.Connection | None = None

    def __post_init__(self) -> None:
        self.tree = self.tree.resolve()
        self.calls: list[dict[str, Any]] = []

    def run(self, name: str, arguments: dict[str, Any]) -> str:
        """Dispatch one call. Every failure is reported to the model, not raised.

        A tool that raises ends the conversation; a tool that explains itself
        lets the model correct course, which is the behaviour worth having when
        the caller is a language model guessing at paths.
        """
        handlers = {
            "read_file": self._read_file,
            "grep": self._grep,
            "list_dir": self._list_dir,
            "callers_of": self._callers_of,
            "tests_for_module": self._tests_for_module,
        }
        handler = handlers.get(name)
        if handler is None:
            self._record(name, arguments, "unknown_tool")
            return f"No tool named {name!r}. Available: {', '.join(sorted(TOOL_NAMES))}."
        try:
            result = handler(arguments)
        except ScopeError as exc:
            self._record(name, arguments, "out_of_scope")
            return f"Refused: {exc}"
        except (OSError, UnicodeDecodeError, re.error, sqlite3.Error) as exc:
            self._record(name, arguments, "failed")
            return f"{type(exc).__name__}: {exc}"
        self._record(name, arguments, "ok")
        return result

    # -- tools ------------------------------------------------------------

    def _read_file(self, arguments: dict[str, Any]) -> str:
        """A window, with the line numbers needed to ask for the next one."""
        path = self._resolve(str(arguments.get("path") or ""))
        if not path.is_file():
            return f"{arguments.get('path')} is not a file in this tree."
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        try:
            start = max(1, int(arguments.get("start_line") or 1))
        except (TypeError, ValueError):
            start = 1
        window = lines[start - 1 : start - 1 + _WINDOW_LINES]
        body = "\n".join(f"{start + offset}: {line}" for offset, line in enumerate(window))
        if len(body) > _FILE_BUDGET:
            body = body[:_FILE_BUDGET] + "\n... window truncated ..."
        end = start + len(window) - 1
        header = f"# {path.relative_to(self.tree).as_posix()} lines {start}-{end} of {len(lines)}"
        if end < len(lines):
            header += f" (ask again with start_line={end + 1} for more)"
        return f"{header}\n{body}"

    def _grep(self, arguments: dict[str, Any]) -> str:
        pattern = re.compile(str(arguments.get("pattern") or ""))
        glob = str(arguments.get("glob") or "*.py")
        matches: list[str] = []
        for path in sorted(self.tree.rglob(glob)):
            if not path.is_file() or self._hidden(path):
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            relative = path.relative_to(self.tree).as_posix()
            for number, line in enumerate(lines, start=1):
                if pattern.search(line):
                    matches.append(f"{relative}:{number}: {line.strip()[:200]}")
                    if len(matches) >= _MATCH_LIMIT:
                        matches.append(f"... stopped at {_MATCH_LIMIT} matches ...")
                        return "\n".join(matches)
        return "\n".join(matches) or "No matches."

    def _list_dir(self, arguments: dict[str, Any]) -> str:
        path = self._resolve(str(arguments.get("path") or "."))
        if not path.is_dir():
            return f"{arguments.get('path')} is not a directory in this tree."
        entries = []
        for entry in sorted(path.iterdir())[:_ENTRY_LIMIT]:
            if self._hidden(entry):
                continue
            entries.append(f"{entry.name}/" if entry.is_dir() else entry.name)
        return "\n".join(entries) or "(empty)"

    def _callers_of(self, arguments: dict[str, Any]) -> str:
        if self.index is None:
            return "The repository index is unavailable for this run."
        from stress_stack.index import callers_of

        rows = callers_of(self.index, str(arguments.get("symbol_id") or ""))
        if not rows:
            return "No recorded references. The symbol id may be wrong; ids look like 'pkg/mod.py::Name'."
        return "\n".join(
            f"{row['kind']} from {row['source']} at {row['path']}:{row['line']}"
            for row in rows[:_MATCH_LIMIT]
        )

    def _tests_for_module(self, arguments: dict[str, Any]) -> str:
        if self.index is None:
            return "The repository index is unavailable for this run."
        from stress_stack.index import tests_for_module

        tests = tests_for_module(self.index, str(arguments.get("module") or ""))
        if not tests:
            return "No test is recorded as executing that module."
        listed = tests[:_MATCH_LIMIT]
        suffix = "" if len(tests) == len(listed) else f"\n... and {len(tests) - len(listed)} more ..."
        return "\n".join(listed) + suffix

    # -- scope ------------------------------------------------------------

    def _resolve(self, candidate: str) -> Path:
        """Resolve inside the tree, or refuse.

        ``resolve()`` before the containment test, not after: it is what turns
        ``a/../../etc`` and a symlink pointing outside into their real
        destinations, and the check is worthless against either without it.
        """
        target = (self.tree / candidate.lstrip("/")).resolve()
        if target != self.tree and self.tree not in target.parents:
            raise ScopeError(
                f"{candidate!r} resolves outside the task tree. "
                "Only paths inside the pre-change tree are readable."
            )
        return target

    def _hidden(self, path: Path) -> bool:
        """Hidden *within the tree* — never by where the tree happens to live.

        Testing the absolute path meant every file was hidden on a real run: the
        staged trees sit under ``.stress_stack/tasks/<id>/input``, so the leading
        dot in an ancestor directory matched everything. ``grep`` and
        ``list_dir`` returned "No matches" and "(empty)" for the whole
        repository while looking like they had genuinely found nothing.
        """
        try:
            parts = path.relative_to(self.tree).parts
        except ValueError:
            return True
        return any(part.startswith(".") or part == "__pycache__" for part in parts)

    def _record(self, name: str, arguments: dict[str, Any], outcome: str) -> None:
        self.calls.append({"tool": name, "arguments": arguments, "outcome": outcome})

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_calls": len(self.calls),
            "tools_used": sorted({call["tool"] for call in self.calls}),
            "refused": [c for c in self.calls if c["outcome"] == "out_of_scope"],
            "calls": self.calls,
        }
