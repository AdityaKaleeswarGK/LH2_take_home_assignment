"""Per-test coverage attribution for non-Python ecosystems.

Excision candidates are ranked on which tests exercise which symbol, so the
question is not "is this line covered" but "covered *by which test*". Python
answers it with coverage.py's dynamic contexts in a single run. No other
ecosystem has an equivalent, so the attribution is obtained the only way
available: run each test alone with coverage on, and record what it reached.

That is one process per test, which is honest about its cost — it is linear in
the number of tests, and on a large suite it is the most expensive stage in the
pipeline. ``max_tests`` bounds it, and the map records how many tests were
actually measured so a truncated attribution is never mistaken for a complete
one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stress_stack.coverage_map import CoverageMap, CoveredSymbol
from stress_stack.tooling import run

# `path/file.go:12.34,15.2 3 1` — start line.col, end line.col, statements, hits
_GO_PROFILE = re.compile(
    r"^(?P<path>.+):(?P<start>\d+)\.\d+,(?P<end>\d+)\.\d+\s+\d+\s+(?P<hits>\d+)$"
)


@dataclass
class AttributionResult:
    status: str
    lines: dict[str, dict[int, list[str]]] = field(default_factory=dict)
    tests_measured: int = 0
    tests_total: int = 0
    reason: str = ""

    @property
    def complete(self) -> bool:
        return self.tests_measured == self.tests_total and self.tests_total > 0


def _go_test_ids(root: Path) -> list[tuple[str, str]]:
    """Every test in the module, as (package, name)."""
    result = run(["go", "test", "-list", ".*", "./..."], cwd=root, timeout=900.0)
    found: list[tuple[str, str]] = []
    pending: list[str] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        # `-list` prints the names first and then a summary line naming the
        # package they came from, so names are held until that line arrives.
        # Attributing them to the *previous* package produced ids like
        # `::TestAdd`, which then matched nothing in the run report.
        if stripped.startswith(("ok ", "?", "FAIL")):
            parts = stripped.split()
            package = parts[1] if len(parts) > 1 else ""
            found.extend((package, name) for name in pending)
            pending = []
            continue
        if stripped.startswith(("Test", "Benchmark", "Fuzz", "Example")):
            pending.append(stripped)
    found.extend(("", name) for name in pending)
    return found


def _parse_go_profile(root: Path, profile: Path) -> dict[str, set[int]]:
    """Which lines a single profile records as executed."""
    covered: dict[str, set[int]] = {}
    if not profile.is_file():
        return covered
    module = _go_module_path(root)
    for line in profile.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _GO_PROFILE.match(line.strip())
        if not match or int(match.group("hits")) == 0:
            continue
        raw = match.group("path")
        # Profile paths are module-qualified; make them repository-relative.
        relative = raw[len(module) :].lstrip("/") if module and raw.startswith(module) else raw
        rows = covered.setdefault(relative, set())
        rows.update(range(int(match.group("start")), int(match.group("end")) + 1))
    return covered


def _go_module_path(root: Path) -> str:
    go_mod = root / "go.mod"
    if not go_mod.is_file():
        return ""
    for line in go_mod.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("module "):
            return line.split(None, 1)[1].strip()
    return ""


def attribute_go(root: Path, *, max_tests: int = 60) -> AttributionResult:
    """Run each Go test alone with coverage and record what it reached."""
    tests = _go_test_ids(root)
    if not tests:
        return AttributionResult("unavailable", reason="no_tests_listed")

    workspace = root / ".stress_stack" / "coverage"
    workspace.mkdir(parents=True, exist_ok=True)
    lines: dict[str, dict[int, list[str]]] = {}
    measured = 0

    for package, name in tests[:max_tests]:
        profile = workspace / f"{name}.out"
        profile.unlink(missing_ok=True)
        outcome = run(
            [
                "go",
                "test",
                f"-run=^{re.escape(name)}$",
                "-count=1",
                # Cover the whole module, not just the package under test, so a
                # test that exercises another package is attributed correctly.
                "-coverpkg=./...",
                f"-coverprofile={profile}",
                "./...",
            ],
            cwd=root,
            timeout=600.0,
        )
        if not outcome.ok and not profile.is_file():
            continue
        measured += 1
        test_id = f"{package}::{name}" if package else name
        for path, rows in _parse_go_profile(root, profile).items():
            per_file = lines.setdefault(path, {})
            for row in rows:
                per_file.setdefault(row, []).append(test_id)

    if measured == 0:
        return AttributionResult(
            "unavailable", tests_total=len(tests), reason="no_test_produced_a_profile"
        )
    return AttributionResult(
        "available", lines=lines, tests_measured=measured, tests_total=len(tests)
    )


_ATTRIBUTORS = {"go": attribute_go}


def build_coverage_map(graph: Any, attribution: AttributionResult) -> CoverageMap:
    """Attribute covered lines to the tree-sitter symbol that owns them.

    Mirrors ``coverage_map.build`` but reads a ``MultiLanguageGraph``, whose
    symbols carry the exact body ranges tree-sitter reported.
    """
    if attribution.status != "available":
        return CoverageMap(status="unavailable", reason=attribution.reason)

    coverage = CoverageMap(status="available")
    for parsed in graph.files:
        per_file = attribution.lines.get(parsed.path, {})
        for symbol in parsed.symbols:
            symbol_id = f"{parsed.path}::{symbol.name}"
            span = range(symbol.first_body_line, symbol.last_body_line + 1)
            covered_rows = [row for row in span if row in per_file]
            tests: set[str] = set()
            for row in covered_rows:
                tests.update(per_file[row])
            coverage.symbols[symbol_id] = CoveredSymbol(
                symbol_id=symbol_id,
                path=parsed.path,
                kind=symbol.kind,
                qualified_name=symbol.name,
                body_lines=max(1, symbol.last_body_line - symbol.first_body_line + 1),
                covered_lines=len(covered_rows),
                covering_tests=sorted(tests),
                # Tree-sitter reports no docstring for these grammars, so this
                # stays False rather than being guessed from a preceding comment.
                has_docstring=False,
                is_test=symbol.is_test,
            )
    return coverage


def measure(root: Path | str, language: str, graph: Any, *, max_tests: int = 60) -> CoverageMap:
    """Per-test coverage for `language`, or an unavailable map explaining why."""
    attributor = _ATTRIBUTORS.get(language)
    if attributor is None:
        return CoverageMap(
            status="unavailable",
            reason=f"no_per_test_coverage_for_{language}",
        )
    attribution = attributor(Path(root), max_tests=max_tests)
    return build_coverage_map(graph, attribution)
