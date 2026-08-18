from __future__ import annotations

import os
import time
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stress_stack.tooling import run

_NO_TESTS_COLLECTED = 5


@dataclass(frozen=True, slots=True)
class PytestRunResult:
    status: str
    reason: str | None
    outcomes: dict[str, str]
    exit_code: int | None
    duration_seconds: float

    def node_ids_with(self, outcome: str) -> set[str]:
        return {node for node, value in self.outcomes.items() if value == outcome}

    @property
    def passing(self) -> set[str]:
        return self.node_ids_with("passed")

    def counts(self) -> dict[str, int]:
        summary: dict[str, int] = {}
        for value in self.outcomes.values():
            summary[value] = summary.get(value, 0) + 1
        return summary

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "exit_code": self.exit_code,
            "duration_seconds": self.duration_seconds,
            "total": len(self.outcomes),
            "counts": self.counts(),
            "outcomes": dict(sorted(self.outcomes.items())),
        }


def run_pytest(
    repository_root: Path,
    python: Path,
    report_path: Path,
    *,
    timeout: float = 900.0,
) -> PytestRunResult:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.unlink(missing_ok=True)
    start = time.monotonic()
    result = run(
        [
            str(python),
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--tb=no",
            "-q",
            f"--junit-xml={report_path}",
        ],
        cwd=repository_root,
        env=environment_for(repository_root, python),
        timeout=timeout,
    )
    duration = round(time.monotonic() - start, 3)

    outcomes = _parse_report(report_path)
    if result.exit_code == _NO_TESTS_COLLECTED and not outcomes:
        return PytestRunResult(
            status="no_tests",
            reason="pytest_collected_no_tests",
            outcomes={},
            exit_code=result.exit_code,
            duration_seconds=duration,
        )
    if not outcomes:
        return PytestRunResult(
            status="unavailable",
            reason=f"pytest_exit_{result.exit_code}: {result.failure_detail()}",
            outcomes={},
            exit_code=result.exit_code,
            duration_seconds=duration,
        )
    reason = None if result.exit_code in {0, 1} else f"pytest_exit_{result.exit_code}"
    return PytestRunResult(
        status="ran",
        reason=reason,
        outcomes=outcomes,
        exit_code=result.exit_code,
        duration_seconds=duration,
    )


def environment_for(repository_root: Path, python: Path) -> dict[str, str]:
    """Expose the repository as importable source and its console scripts on PATH.

    Tests that shell out to an entry point (``subprocess.check_output(["glom", ...])``)
    fail with FileNotFoundError unless the environment's scripts directory is on PATH.

    ``PYTHONPATH`` is derived by :func:`stress_stack.runner.source_roots` rather
    than set to the repository root. Under a ``src/`` layout the root holds no
    importable package, so the bare root left the installed copy winning — host
    runs measured site-packages while container runs measured the source tree,
    and ``compare_to_baseline`` then compared two different things.
    """
    from stress_stack.runner import source_roots

    existing_path = os.environ.get("PATH", "")
    return {
        "PYTHONPATH": source_roots(repository_root, mount=None),
        "PATH": f"{python.parent}{os.pathsep}{existing_path}" if existing_path else str(python.parent),
    }


def compare(before: PytestRunResult, after: PytestRunResult) -> dict[str, list[str]]:
    regressions = sorted(before.passing - after.passing)
    repairs = sorted(after.passing - before.passing)
    disappeared = sorted(set(before.outcomes) - set(after.outcomes))
    appeared = sorted(set(after.outcomes) - set(before.outcomes))
    return {
        "regressions": regressions,
        "repairs": repairs,
        "disappeared": disappeared,
        "appeared": appeared,
    }


def _parse_report(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        tree = ElementTree.parse(path)
    except ElementTree.ParseError:
        return {}
    outcomes: dict[str, str] = {}
    for case in tree.iter("testcase"):
        outcomes[_node_id(case)] = _outcome(case)
    return outcomes


def _node_id(case: ElementTree.Element) -> str:
    name = case.get("name") or "<unnamed>"
    class_name = case.get("classname") or ""
    return f"{class_name}::{name}" if class_name else name


def _outcome(case: ElementTree.Element) -> str:
    for child in case:
        if child.tag == "failure":
            return "failed"
        if child.tag == "error":
            return "error"
        if child.tag == "skipped":
            return "skipped"
    return "passed"
