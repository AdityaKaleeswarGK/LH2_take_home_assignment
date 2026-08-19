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


# LCOV is the one coverage format every Rust tool agrees on. `SF:` opens a file
# section, `DA:<line>,<hits>` records a line, `end_of_record` closes it.
_LCOV_FILE = re.compile(r"^SF:(?P<path>.+)$")
_LCOV_LINE = re.compile(r"^DA:(?P<line>\d+),(?P<hits>\d+)")


def _rust_test_ids(root: Path) -> list[tuple[str, str]]:
    """Every test in the workspace, as (target, name).

    ``--format=terse`` prints ``name: test`` per line, which is stable across
    the versions of libtest anyone is likely to be running.
    """
    result = run(
        ["cargo", "test", "--all-targets", "--", "--list", "--format=terse"],
        cwd=root,
        timeout=900.0,
    )
    found: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped.endswith(": test"):
            continue
        name = stripped[: -len(": test")].strip()
        if name:
            found.append(("", name))
    return found


def _parse_lcov(root: Path, report: Path) -> dict[str, set[int]]:
    """Which lines an LCOV report records as executed, repository-relative."""
    covered: dict[str, set[int]] = {}
    if not report.is_file():
        return covered
    resolved = root.resolve()
    current: set[int] | None = None
    for raw in report.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        opened = _LCOV_FILE.match(line)
        if opened:
            path = Path(opened.group("path"))
            try:
                relative = str((resolved / path).resolve().relative_to(resolved))
            except ValueError:
                # A dependency compiled from outside the workspace. Dropping it
                # is right — it cannot be attributed to a symbol in this graph.
                current = None
                continue
            current = covered.setdefault(relative, set())
            continue
        if line == "end_of_record":
            current = None
            continue
        if current is None:
            continue
        hit = _LCOV_LINE.match(line)
        if hit and int(hit.group("hits")) > 0:
            current.add(int(hit.group("line")))
    return covered


def attribute_rust(root: Path, *, max_tests: int = 60) -> AttributionResult:
    """Run each Rust test alone under llvm-cov and record what it reached.

    Same shape as the Go attributor and the same honest cost: one process per
    test. Rust had a test plan but no attributor, which made it a dead end —
    ``mine_excision`` needs a coverage map, so a Rust repository produced no
    candidates and the run stopped at a MetadataError rather than at anything
    explaining why.

    ``cargo llvm-cov`` is a separate installation. Its absence is reported, not
    worked around: a coverage map assembled without it would be empty, and an
    empty map that claims to be available is how excision mining gets nothing
    and no reason.
    """
    probe = run(["cargo", "llvm-cov", "--version"], cwd=root, timeout=120.0)
    if not probe.ok:
        return AttributionResult(
            "unavailable",
            reason="cargo_llvm_cov_not_installed: install with `cargo install cargo-llvm-cov`",
        )

    tests = _rust_test_ids(root)
    if not tests:
        return AttributionResult("unavailable", reason="no_tests_listed")

    workspace = root / ".stress_stack" / "coverage"
    workspace.mkdir(parents=True, exist_ok=True)
    lines: dict[str, dict[int, list[str]]] = {}
    measured = 0

    for _target, name in tests[:max_tests]:
        report = workspace / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', name)}.lcov"
        report.unlink(missing_ok=True)
        outcome = run(
            [
                "cargo", "llvm-cov",
                "--lcov", f"--output-path={report}",
                "test", "--",
                "--exact", name,
            ],
            cwd=root,
            timeout=600.0,
        )
        if not outcome.ok and not report.is_file():
            continue
        measured += 1
        for path, rows in _parse_lcov(root, report).items():
            per_file = lines.setdefault(path, {})
            for row in rows:
                per_file.setdefault(row, []).append(name)

    if measured == 0:
        return AttributionResult(
            "unavailable", tests_total=len(tests), reason="no_test_produced_a_report"
        )
    return AttributionResult(
        "available", lines=lines, tests_measured=measured, tests_total=len(tests)
    )


_ATTRIBUTORS = {"go": attribute_go, "rust": attribute_rust}

# How each report format is produced and read back. The commands come from the
# workflow record; this is the part that has to be code, because a format is a
# parser and because the flag that names an output path differs per tool.
#
# `units` is what the attribution is *per*. Go and Rust list individual tests, so
# a unit is a test. v8 collects coverage for a whole vitest process with no
# equivalent of coverage.py's dynamic contexts, so a unit is a test *file* — and
# the map records how many units it measured, so a coarser attribution is
# reported as what it is rather than passed off as per-test.
@dataclass(frozen=True)
class _Format:
    reader: Any
    report: Any
    report_args: Any
    filter_args: Any
    units: Any


def _go_test_names(stdout: str) -> list[str]:
    names: list[str] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(("Test", "Benchmark", "Fuzz", "Example")):
            names.append(stripped)
    return names


def _libtest_names(stdout: str) -> list[str]:
    return [
        line.strip()[: -len(": test")].strip()
        for line in stdout.splitlines()
        if line.strip().endswith(": test")
    ]


def _vitest_files(stdout: str) -> list[str]:
    """Unique test files from `vitest list`, whose lines are `file > suite > test`."""
    files: list[str] = []
    for line in stdout.splitlines():
        head = line.strip().split(" > ", 1)[0].strip()
        if head and (head.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"))
                     and head not in files):
            files.append(head)
    return files


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


_FORMATS: dict[str, _Format] = {
    "go_profile": _Format(
        reader=_parse_go_profile,
        report=lambda workspace, unit: workspace / f"{_safe(unit)}.out",
        report_args=lambda report: [f"-coverprofile={report}"],
        filter_args=lambda unit: [f"-run=^{re.escape(unit)}$", "-count=1", "./..."],
        units=_go_test_names,
    ),
    "lcov": _Format(
        reader=_parse_lcov,
        report=lambda workspace, unit: workspace / f"{_safe(unit)}.lcov",
        report_args=lambda report: [f"--output-path={report}"],
        filter_args=lambda unit: ["--", "--exact", unit],
        units=_libtest_names,
    ),
    # vitest writes lcov.info *inside* a directory it is given, so the path the
    # reader gets is not the path the tool is told.
    "lcov_v8": _Format(
        reader=lambda root, report: _parse_lcov(root, report / "lcov.info"),
        report=lambda workspace, unit: workspace / _safe(unit),
        report_args=lambda report: [
            "--coverage.reporter=lcov",
            f"--coverage.reportsDirectory={report}",
        ],
        filter_args=lambda unit: [unit],
        units=_vitest_files,
    ),
}


def attribute_by_workflow(root: Path, record: Any, *, max_tests: int = 60) -> AttributionResult:
    """Per-unit attribution driven by a resolved workflow record.

    Same shape as the two hand-written attributors above and the same honest
    cost — one process per unit — but the commands come from the workflow instead
    of from a branch. That is what makes a third ecosystem free: an
    lcov-emitting toolchain needs a record, not a function.

    The two built-ins stay as the defaults a workflow probes first, so Go and
    Rust behave identically whether or not a model was ever consulted.
    """
    spec = _FORMATS.get(str(record.settings.get("format") or ""))
    listing = record.command("list")
    measure_argv = record.command("measure")
    if spec is None or not listing or not measure_argv:
        return AttributionResult("unavailable", reason="workflow_record_incomplete")

    listed = run(listing, cwd=root, timeout=900.0)
    units = spec.units(listed.stdout)
    if not units:
        return AttributionResult("unavailable", reason="no_tests_listed")

    workspace = root / ".stress_stack" / "coverage"
    workspace.mkdir(parents=True, exist_ok=True)
    lines: dict[str, dict[int, list[str]]] = {}
    measured = 0

    for unit in units[:max_tests]:
        report = spec.report(workspace, unit)
        if report.is_file():
            report.unlink()
        # Arguments are appended as a vector rather than interpolated: the
        # record's command was checked for shell metacharacters, and keeping
        # them separate means a test name out of the repository cannot become
        # one.
        outcome = run(
            [*measure_argv, *spec.report_args(report), *spec.filter_args(unit)],
            cwd=root,
            timeout=600.0,
        )
        covered = spec.reader(root, report)
        if not outcome.ok and not covered:
            continue
        measured += 1
        for path, rows in covered.items():
            per_file = lines.setdefault(path, {})
            for row in rows:
                per_file.setdefault(row, []).append(unit)

    if measured == 0:
        return AttributionResult(
            "unavailable", tests_total=len(units), reason="no_test_produced_a_report"
        )
    return AttributionResult(
        "available", lines=lines, tests_measured=measured, tests_total=len(units)
    )


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


def measure(
    root: Path | str,
    language: str,
    graph: Any,
    *,
    max_tests: int = 60,
    workflow_record: Any = None,
) -> CoverageMap:
    """Per-test coverage for `language`, or an unavailable map explaining why.

    A resolved workflow record wins over the built-in attributor, because a
    record only exists once its probe attributed a symbol — whereas the table
    below covers the two ecosystems somebody wrote a function for. Without a
    record the behaviour is exactly what it was.
    """
    if workflow_record is not None:
        attribution = attribute_by_workflow(Path(root), workflow_record, max_tests=max_tests)
        return build_coverage_map(graph, attribution)

    attributor = _ATTRIBUTORS.get(language)
    if attributor is None:
        return CoverageMap(
            status="unavailable",
            reason=f"no_per_test_coverage_for_{language}",
        )
    attribution = attributor(Path(root), max_tests=max_tests)
    return build_coverage_map(graph, attribution)
