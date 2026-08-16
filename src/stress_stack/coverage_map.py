"""Measured test-to-symbol coverage.

Static "this test file imports that module" is a guess. ``coverage.py`` contexts
record which test actually executed which line, so the mapping is observed
rather than inferred — and it is the only evidence that makes an excision
candidate defensible: a function nothing executes cannot be excised into a task
whose verifier fails for a behavioural reason.

``coverage`` itself is never imported here. It runs inside the repository's own
test environment and is queried through a probe, the same way distribution
metadata is read in :mod:`stress_stack.dependencies`, so this package keeps zero
runtime dependencies.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stress_stack.graph import RepositoryGraph
from stress_stack.symbols import ParsedSymbol
from stress_stack.tooling import install, run

# Coverage count is not monotonic quality. A symbol executed by 119 tests is
# load-bearing infrastructure: excising it yields "rewrite the core" plus huge
# collateral. One covering test either over-constrains (couples to the
# implementation) or under-constrains (a stub passes). The usable band sits
# between.
#
# The floor is a principle and stays fixed: with a single covering test there is
# nothing to triangulate a correct alternative implementation against.
#
# The ceiling is a fact about a repository, so it is measured rather than
# declared. "12 covering tests" was true of glom and of nothing else; on a
# repository with broader integration tests it would reject every candidate.
#
# It is expressed as a share of the suite rather than as a percentile of the
# observed breadth. A percentile always removes its top slice, so a repository
# where nothing is infrastructural would still lose its best candidates. What
# actually marks a symbol as load-bearing is how much of the suite runs through
# it: glom's two rejected symbols are executed by 100 and 119 of 202 tests,
# while the next widest is 28. One test in seven is the line.
_MIN_TESTS_FOR_EXCISION = 2
_INFRASTRUCTURE_SHARE = 0.15
_FALLBACK_CEILING = 12
_MIN_RATIO_FOR_EXCISION = 0.6
_MIN_BODY_LINES_FOR_EXCISION = 4

_EXTRACT = """
import json, sys
import coverage

data = coverage.CoverageData(sys.argv[1])
data.read()
out = {}
for measured in data.measured_files():
    by_line = data.contexts_by_lineno(measured)
    rows = {}
    for line, contexts in by_line.items():
        named = sorted(c for c in contexts if c)
        if named:
            rows[str(line)] = named
    if rows:
        out[measured] = rows
print(json.dumps(out))
"""


@dataclass
class CoveredSymbol:
    symbol_id: str
    path: str
    kind: str
    qualified_name: str
    body_lines: int
    covered_lines: int = 0
    covering_tests: list[str] = field(default_factory=list)
    has_docstring: bool = False
    covering_ceiling: int = _FALLBACK_CEILING
    """Breadth above which a symbol counts as infrastructure, set by the map."""

    @property
    def ratio(self) -> float:
        return round(self.covered_lines / self.body_lines, 3) if self.body_lines else 0.0

    @property
    def excision_ready(self) -> bool:
        """A cheap pre-filter, not the gate.

        The real test is empirical: remove the body and confirm the covering
        tests fail on behaviour. This only avoids spending a validation run on
        a function whose contract or coverage is obviously too thin.
        """
        return (
            _MIN_TESTS_FOR_EXCISION <= len(self.covering_tests) <= self.covering_ceiling
            and self.has_docstring
            and self.ratio >= _MIN_RATIO_FOR_EXCISION
            and self.body_lines >= _MIN_BODY_LINES_FOR_EXCISION
            and self.kind in {"function", "method"}
        )

    @property
    def focus_score(self) -> float:
        """Rank within the band: well-exercised, but not infrastructure."""
        if not self.excision_ready:
            return 0.0
        span = max(1, self.covering_ceiling - _MIN_TESTS_FOR_EXCISION)
        centrality = 1.0 - (len(self.covering_tests) - _MIN_TESTS_FOR_EXCISION) / span
        return round(self.ratio * 0.6 + centrality * 0.4, 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol_id": self.symbol_id,
            "path": self.path,
            "kind": self.kind,
            "qualified_name": self.qualified_name,
            "body_lines": self.body_lines,
            "covered_lines": self.covered_lines,
            "ratio": self.ratio,
            "has_docstring": self.has_docstring,
            "covering_tests": sorted(self.covering_tests),
            "covering_ceiling": self.covering_ceiling,
            "excision_ready": self.excision_ready,
            "focus_score": self.focus_score,
        }


@dataclass
class CoverageMap:
    status: str
    reason: str | None = None
    symbols: dict[str, CoveredSymbol] = field(default_factory=dict)

    def tests_index(self) -> dict[str, list[str]]:
        index: dict[str, list[str]] = {}
        for symbol in self.symbols.values():
            for test in symbol.covering_tests:
                index.setdefault(test, []).append(symbol.symbol_id)
        return {test: sorted(ids) for test, ids in sorted(index.items())}

    def covering(self, symbol_id: str) -> list[str]:
        symbol = self.symbols.get(symbol_id)
        return sorted(symbol.covering_tests) if symbol else []

    def excision_candidates(self) -> list[CoveredSymbol]:
        return sorted(
            (s for s in self.symbols.values() if s.excision_ready),
            key=lambda s: (-s.focus_score, s.symbol_id),
        )

    def calibrate(self) -> int:
        """Set the infrastructure ceiling from this repository's own suite size.

        Scale-free by construction: a symbol is infrastructure when too much of
        the suite runs through it, which means the same thing in a repository of
        forty tests and one of four thousand.
        """
        observed = len(self.tests_index())
        ceiling = (
            _FALLBACK_CEILING
            if not observed
            else max(_MIN_TESTS_FOR_EXCISION + 1, round(_INFRASTRUCTURE_SHARE * observed))
        )
        for symbol in self.symbols.values():
            symbol.covering_ceiling = ceiling
        return ceiling

    def statistics(self) -> dict[str, int | float]:
        covered = [s for s in self.symbols.values() if s.covering_tests]
        ceilings = {s.covering_ceiling for s in self.symbols.values()}
        return {
            "symbols_total": len(self.symbols),
            "symbols_covered": len(covered),
            "tests_observed": len(self.tests_index()),
            "excision_candidates": len(self.excision_candidates()),
            "covering_ceiling": max(ceilings) if ceilings else _FALLBACK_CEILING,
            "mean_tests_per_covered_symbol": (
                round(sum(len(s.covering_tests) for s in covered) / len(covered), 2)
                if covered
                else 0.0
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "0.1.0",
            "status": self.status,
            "reason": self.reason,
            "statistics": self.statistics(),
            "tests": self.tests_index(),
            "symbols": {
                key: value.to_dict()
                for key, value in sorted(self.symbols.items())
                if value.covering_tests
            },
        }


def normalize_context(context: str) -> str:
    """Turn a coverage context into the pytest node id the gates already use.

    coverage reports ``pkg.test_mod.test_name`` (and may append ``|setup`` or
    ``|teardown``); JUnit reports ``pkg.test_mod::test_name``. Aligning them is
    what lets a covering test be handed straight to the verifier.
    """
    text = context.split("|", 1)[0].strip()
    if not text or "::" in text:
        return text
    module, separator, name = text.rpartition(".")
    return f"{module}::{name}" if separator else text


def collect(
    repository_root: Path,
    python: Path,
    evidence_dir: Path,
    *,
    packages: list[str],
    timeout: float = 1800.0,
) -> tuple[dict[str, dict[int, list[str]]], str, str | None]:
    """Run the suite once with per-test contexts and return line-level coverage."""
    evidence_dir.mkdir(parents=True, exist_ok=True)
    datafile = evidence_dir / "coverage.data"
    datafile.unlink(missing_ok=True)

    installation = install(python, ["pytest-cov"])
    if not installation.ok:
        return {}, "unavailable", f"pytest_cov_install_failed: {installation.failure_detail()}"

    arguments = [
        str(python),
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "--tb=no",
        "-q",
        "--cov-context=test",
        "--cov-report=",
    ]
    arguments += [f"--cov={name}" for name in packages] or ["--cov=."]
    result = run(
        arguments,
        cwd=repository_root,
        env={
            "PYTHONPATH": str(repository_root),
            "COVERAGE_FILE": str(datafile),
        },
        timeout=timeout,
    )
    if not datafile.exists():
        return {}, "unavailable", f"no_coverage_data: {result.failure_detail()}"

    extraction = run([str(python), "-c", _EXTRACT, str(datafile)], timeout=300.0)
    if not extraction.ok:
        return {}, "unavailable", f"coverage_read_failed: {extraction.failure_detail()}"
    try:
        raw = json.loads(extraction.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        return {}, "unavailable", f"coverage_parse_failed: {exc}"

    relative: dict[str, dict[int, list[str]]] = {}
    root = repository_root.resolve()
    for measured, rows in raw.items():
        try:
            path = str(Path(measured).resolve().relative_to(root))
        except ValueError:
            continue
        relative[path] = {int(line): contexts for line, contexts in rows.items()}
    return relative, "available", None


def build(graph: RepositoryGraph, lines: dict[str, dict[int, list[str]]]) -> CoverageMap:
    """Attribute covered lines to the innermost symbol that owns them."""
    coverage_map = CoverageMap(status="available")
    for parsed in graph.files:
        owners = _line_owners(parsed.symbols)
        for symbol in parsed.symbols:
            if symbol.kind == "module":
                continue
            coverage_map.symbols[symbol.id] = CoveredSymbol(
                symbol_id=symbol.id,
                path=parsed.path,
                kind=symbol.kind,
                qualified_name=symbol.qualified_name,
                body_lines=max(1, symbol.anchor.end_line - symbol.anchor.line),
                has_docstring=bool(symbol.docstring),
            )
        for line, contexts in lines.get(parsed.path, {}).items():
            owner = owners.get(line)
            if owner is None:
                continue
            record = coverage_map.symbols.get(owner)
            if record is None:
                continue
            record.covered_lines += 1
            for context in contexts:
                test_id = normalize_context(context)
                if test_id and test_id not in record.covering_tests:
                    record.covering_tests.append(test_id)
    coverage_map.calibrate()
    return coverage_map


def load(path: Path) -> CoverageMap:
    """Re-read a written coverage map.

    Only symbols with covering tests are persisted, so a reloaded map is the
    covered subset — which is all any consumer of it asks about. Reloading
    rather than re-measuring is what keeps mining a seconds-long operation
    against a suite that takes minutes.

    Recalibration reproduces the ceiling exactly rather than trusting the stored
    one, and it can: the band is drawn from symbols with two or more covering
    tests, every one of which is persisted.
    """
    if not path.is_file():
        return CoverageMap(status="unavailable", reason="no_coverage_map")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CoverageMap(status="unavailable", reason=f"unreadable_coverage_map: {exc}")

    coverage_map = CoverageMap(status=str(payload.get("status") or "available"))
    for symbol_id, record in (payload.get("symbols") or {}).items():
        coverage_map.symbols[symbol_id] = CoveredSymbol(
            symbol_id=symbol_id,
            path=str(record.get("path") or ""),
            kind=str(record.get("kind") or ""),
            qualified_name=str(record.get("qualified_name") or ""),
            body_lines=int(record.get("body_lines") or 1),
            covered_lines=int(record.get("covered_lines") or 0),
            covering_tests=list(record.get("covering_tests") or []),
            has_docstring=bool(record.get("has_docstring")),
        )
    coverage_map.calibrate()
    return coverage_map


def _line_owners(symbols: list[ParsedSymbol]) -> dict[int, str]:
    """Map each line to its innermost enclosing symbol.

    A method's lines also fall inside its class and its module; attributing them
    to the outermost span would make every class look fully covered.
    """
    owners: dict[int, str] = {}
    ordered = sorted(
        (s for s in symbols if s.kind != "module"),
        key=lambda s: s.anchor.end_line - s.anchor.line,
        reverse=True,
    )
    for symbol in ordered:
        for line in range(symbol.anchor.line, symbol.anchor.end_line + 1):
            owners[line] = symbol.id
    return owners
