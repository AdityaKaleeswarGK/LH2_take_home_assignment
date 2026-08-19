"""Tests for the agentic project-aware multi-language architecture."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from stress_stack.ci_parser import CIParsedFacts
from stress_stack.container_doctor import (
    _shell_command,
    run_container_verification,
    synthesize_dockerfile,
)
from stress_stack.dependency_doctor import UNSUPPORTED, _count_go_modules, lock_dependencies
from stress_stack.excision_multilang import excise_symbol
from stress_stack.hygiene_dispatcher import dispatch_hygiene
from stress_stack.parsers.tree_sitter_core import parse_source_code
from stress_stack.project_detector import ProjectProfile, detect_project_profile
from stress_stack.tracker import TaskTracker


def test_tree_sitter_multilang_parsing() -> None:
    # 1. Python
    py_code = """
import os
from math import sqrt

def calculate(x):
    return sqrt(x)

def test_calc():
    assert calculate(4) == 2
"""
    py_parsed = parse_source_code("test_math.py", py_code)
    assert py_parsed.language == "python"
    assert len(py_parsed.imports) == 2
    assert len(py_parsed.symbols) == 2
    assert len(py_parsed.tests) == 1

    # 2. Rust
    rs_code = """
use std::collections::HashMap;

pub fn add(a: i32, b: i32) -> i32 {
    a + b
}

#[test]
fn test_add() {
    assert_eq!(add(2, 2), 4);
}
"""
    rs_parsed = parse_source_code("src/lib.rs", rs_code)
    assert rs_parsed.language == "rust"
    assert len(rs_parsed.imports) == 1
    assert any(s.name == "add" for s in rs_parsed.symbols)
    assert len(rs_parsed.tests) == 1

    # 3. TypeScript
    ts_code = """
import { sum } from './math';

export function calculateTotal(items: number[]): number {
    return items.reduce((a, b) => a + b, 0);
}

describe('calculateTotal', () => {
    it('sums numbers', () => {
        expect(calculateTotal([1, 2])).toBe(3);
    });
});
"""
    ts_parsed = parse_source_code("src/calc.ts", ts_code)
    assert ts_parsed.language == "typescript"
    assert len(ts_parsed.imports) == 1
    assert any(s.name == "calculateTotal" for s in ts_parsed.symbols)
    assert len(ts_parsed.tests) >= 1

    # 4. Go
    go_code = """
package calc

import "fmt"

func Multiply(a int, b int) int {
    return a * b
}

func TestMultiply(t *testing.T) {
    if Multiply(2, 3) != 6 {
        t.Fail()
    }
}
"""
    go_parsed = parse_source_code("calc_test.go", go_code)
    assert go_parsed.language == "go"
    assert len(go_parsed.imports) == 1
    assert any(s.name == "Multiply" for s in go_parsed.symbols)
    assert any(t.name == "TestMultiply" for t in go_parsed.tests)


def test_tree_sitter_is_actually_active() -> None:
    """The grammars must be installed, not silently falling back to regex."""
    from stress_stack.parsers.tree_sitter_core import tree_sitter_available

    assert tree_sitter_available() is True


def test_body_extent_survives_a_brace_inside_a_string() -> None:
    """The reason the grammar matters: regex brace counting cuts the wrong lines.

    `tricky` contains `"}"` in a string literal. Counting braces ends the
    function at that line, so excising it would leave a syntactically broken
    file and a task whose `input/` never compiled.
    """
    source = (
        'pub fn tricky(x: i32) -> String {\n'
        '    let s = "}";\n'
        '    let t = x + 1;\n'
        '    format!("{}{}", s, t)\n'
        '}\n'
        '\n'
        'pub fn after() -> i32 { 42 }\n'
    )

    parsed = parse_source_code("src/lib.rs", source)
    tricky = next(s for s in parsed.symbols if s.name == "tricky")
    assert (tricky.start_line, tricky.end_line) == (1, 5)
    assert parsed.has_syntax_error is False

    # The fallback is kept for environments without grammars, and it is wrong
    # here — which is exactly why it must not be the default path.
    fallback = parse_source_code("src/lib.rs", source, prefer_tree_sitter=False)
    fallback_tricky = next(s for s in fallback.symbols if s.name == "tricky")
    assert fallback_tricky.end_line == 2


def test_javascript_tests_declared_by_call_are_found() -> None:
    """`describe`/`it` register tests by calling, not by declaring."""
    source = (
        "describe('math', () => {\n"
        "  it('adds', () => { expect(1 + 1).toBe(2); });\n"
        "  it.each([1, 2])('handles %i', (n) => { expect(n).toBeTruthy(); });\n"
        "});\n"
    )
    parsed = parse_source_code("src/math.test.js", source)
    names = {t.name for t in parsed.tests}
    assert "adds" in names
    assert "math" in names


def test_multilang_excision() -> None:
    # Python Excision
    py_code = "def foo(x):\n    return x + 1\n"
    py_exc = excise_symbol("foo.py", py_code, "foo")
    assert py_exc is not None
    assert "def foo(x):" in py_exc.stubbed
    assert py_exc.diff() != ""

    # Rust Excision
    rs_code = "pub fn foo(x: i32) -> i32 {\n    x + 1\n}\n"
    rs_exc = excise_symbol("src/foo.rs", rs_code, "foo")
    assert rs_exc is not None
    assert "todo!()" in rs_exc.stubbed
    assert "x + 1" in rs_exc.diff()

    # TypeScript Excision
    ts_code = "export function foo(x: number): number {\n    return x + 1;\n}\n"
    ts_exc = excise_symbol("src/foo.ts", ts_code, "foo")
    assert ts_exc is not None
    assert "throw new Error" in ts_exc.stubbed


def test_project_detector_and_ci_parser() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        # Create GitHub workflow
        wf_dir = root / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "ci.yml").write_text(
            """
name: CI
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: sudo apt-get install -y libssl-dev
      - run: cargo test --workspace
""",
            encoding="utf-8",
        )
        (root / "Cargo.toml").write_text(
            """
[workspace]
members = ["crates/*"]
""",
            encoding="utf-8",
        )

        profile = detect_project_profile(root)
        assert profile.primary_language == "rust"
        assert profile.toolchain == "cargo"
        assert profile.is_monorepo is True
        assert "libssl-dev" in profile.ci_facts.system_packages
        assert "cargo test --workspace" in profile.ci_facts.test_commands


def test_task_tracker_synchronization() -> None:
    tracker = TaskTracker()
    assert not tracker.is_done("task_1")

    tracker.mark_done("task_1", {"status": "ok"})
    assert tracker.is_done("task_1")
    assert tracker.get_result("task_1") == {"status": "ok"}
    assert tracker.wait_for("task_1", timeout=0.1) == "done"


# The regression tests below exist because each of these values was previously
# fabricated: a hardcoded `runs_identical=True`, an "approximate" pin count, and
# an unconditional "complete" status. A doctor may report ignorance; it may not
# report a success it did not measure.


def _profile(language: str, **overrides: object) -> ProjectProfile:
    defaults: dict[str, object] = {
        "root": Path("/nonexistent"),
        "primary_language": language,
        "languages_present": [language],
        "ecosystem": language,
        "toolchain": "unknown",
        "is_monorepo": False,
        "workspace_members": [],
        "default_test_command": "echo test",
        "pre_build_command": None,
        "base_image": "scratch",
        "ci_facts": CIParsedFacts(),
    }
    defaults.update(overrides)
    return ProjectProfile(**defaults)  # type: ignore[arg-type]


def test_lock_reports_unsupported_rather_than_inventing_a_lock() -> None:
    """An ecosystem with no lock strategy must not return a `locked` status."""
    with tempfile.TemporaryDirectory() as tmpdir:
        report = lock_dependencies(Path(tmpdir), _profile("cpp"))

    assert report.status == UNSUPPORTED
    assert report.pinned_count == 0
    assert report.measured is False
    assert "cpp" in report.reason


def test_go_sum_counts_modules_not_lines() -> None:
    """go.sum lists each module twice; counting lines doubles the real count."""
    go_sum = (
        "github.com/x/y v1.2.3 h1:abc=\n"
        "github.com/x/y v1.2.3/go.mod h1:def=\n"
        "github.com/p/q v0.1.0 h1:ghi=\n"
        "github.com/p/q v0.1.0/go.mod h1:jkl=\n"
    )
    assert _count_go_modules(go_sum) == 2


def test_dockerfile_command_survives_quotes_from_ci_yaml() -> None:
    """The test command is untrusted repo input and must not break out of JSON."""
    hostile = 'pytest -k "not slow" && echo "done"'
    rendered = synthesize_dockerfile(_profile("go", default_test_command=hostile), "scratch")

    cmd_line = [line for line in rendered.splitlines() if line.startswith("CMD ")][0]
    payload = json.loads(cmd_line.removeprefix("CMD "))
    assert payload == ["sh", "-c", hostile]


def test_shell_command_is_valid_json_for_embedded_quotes() -> None:
    assert json.loads(_shell_command('a "b" c')) == ["sh", "-c", 'a "b" c']


def test_container_does_not_claim_determinism_without_two_runs(monkeypatch) -> None:
    """With docker absent, `runs_identical` must be None — not True."""
    monkeypatch.setattr("stress_stack.container_doctor.shutil.which", lambda _: None)
    with tempfile.TemporaryDirectory() as tmpdir:
        result = run_container_verification(Path(tmpdir), _profile("rust"))

    assert result.status == "unsupported"
    assert result.runs_identical is None
    assert result.test_runs == []


def test_determinism_verdict_states_its_resolution() -> None:
    """An identical-output verdict must not be read as per-test determinism."""
    with tempfile.TemporaryDirectory() as tmpdir:
        result = run_container_verification(Path(tmpdir), _profile("elixir"))

    assert result.to_dict()["determinism_resolution"] in {"process_output", "per_test"}


def test_hygiene_reports_unsupported_when_formatter_missing(monkeypatch) -> None:
    """No formatter available must not report `complete` with zero regressions."""
    monkeypatch.setattr("stress_stack.hygiene_dispatcher.shutil.which", lambda _: None)
    with tempfile.TemporaryDirectory() as tmpdir:
        report = dispatch_hygiene(Path(tmpdir), _profile("go"))

    assert report.status == "unsupported"
    assert report.regressions is None
    assert report.regressions_verified is False


def test_hygiene_never_claims_verified_regressions_off_python() -> None:
    """Only the Python path measures a before/after snapshot today."""
    with tempfile.TemporaryDirectory() as tmpdir:
        report = dispatch_hygiene(Path(tmpdir), _profile("elixir"))

    assert report.regressions_verified is False
    assert report.before_tests_passing is None


def test_multilanguage_graph_excludes_hidden_duplicate_trees() -> None:
    """A hidden worktree copy must not be described as the repository's own.

    `.claude/worktrees/` holds a full second checkout; counting it doubled every
    statistic and attributed the copy's symbols to the original.
    """
    from stress_stack.graph_multilang import iter_source_files

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "src").mkdir()
        (root / "src" / "main.go").write_text("package m\n\nfunc A() int { return 1 }\n")

        hidden = root / ".claude" / "worktrees" / "copy" / "src"
        hidden.mkdir(parents=True)
        (hidden / "main.go").write_text("package m\n\nfunc A() int { return 1 }\n")

        found = [p.name for p in iter_source_files(root)]

    assert found == ["main.go"]


def test_multilanguage_graph_validates_against_a_reparse() -> None:
    """The knowledge layer must be reproducible from the source it describes."""
    from stress_stack.graph_multilang import build_graph, validate_graph

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "calc.go").write_text(
            'package calc\n\nimport "fmt"\n\n'
            "func Add(a int, b int) int { return a + b }\n\n"
            "func TestAdd(t *testing.T) { _ = fmt.Sprint(Add(1, 2)) }\n"
        )
        graph = build_graph(root)
        report = validate_graph(graph, root)

    assert report["status"] == "verified"
    assert report["edge_match_rate"] == 1.0
    assert report["anchor_match_rate"] == 1.0
    assert graph.statistics()["symbols"] == 2
    assert graph.statistics()["tests"] == 1


def test_tsx_uses_its_own_grammar() -> None:
    """Parsing JSX with the plain TypeScript grammar reports a syntax error."""
    source = (
        "export function Panel({ label }: { label: string }) {\n"
        "  return <div className=\"panel\">{label}</div>;\n"
        "}\n"
    )
    parsed = parse_source_code("src/Panel.tsx", source)

    assert parsed.language == "tsx"
    assert parsed.has_syntax_error is False
    assert any(s.name == "Panel" for s in parsed.symbols)


def test_symbol_names_are_bare_identifiers() -> None:
    """A C++ partial specialisation names itself with its template arguments."""
    source = (
        "template <typename T>\n"
        "struct traits <\n"
        "    T,\n"
        "    enable_if_t<is_integral<T>::value>> {\n"
        "  static const int value = 1;\n"
        "};\n"
    )
    parsed = parse_source_code("json.hpp", source)

    for symbol in parsed.symbols:
        assert "\n" not in symbol.name, f"multi-line symbol name: {symbol.name!r}"
        assert symbol.name.replace("_", "").isalnum() or symbol.name == ""


# --- hygiene verification and revert -----------------------------------------


def test_suite_comparison_detects_behaviour_change() -> None:
    """Formatting must never change what the suite does."""
    from stress_stack.hygiene_verify import AVAILABLE, SuiteSnapshot, compare

    clean = SuiteSnapshot(AVAILABLE, 0, "ok  pkg")
    same = SuiteSnapshot(AVAILABLE, 0, "ok  pkg")
    assert compare(clean, same) == (False, "")

    failing = SuiteSnapshot(AVAILABLE, 1, "ok  pkg")
    regressed, why = compare(clean, failing)
    assert regressed and "exit code" in why

    different = SuiteSnapshot(AVAILABLE, 0, "FAIL  pkg")
    regressed, why = compare(clean, different)
    assert regressed and "output changed" in why


def test_unavailable_snapshot_is_not_a_regression() -> None:
    """Not measuring is different from measuring a change."""
    from stress_stack.hygiene_verify import AVAILABLE, UNAVAILABLE, SuiteSnapshot, compare

    good = SuiteSnapshot(AVAILABLE, 0, "ok")
    missing = SuiteSnapshot(UNAVAILABLE, None, "", "suite_timed_out")

    regressed, why = compare(good, missing)
    assert regressed is False
    assert "not_compared" in why


def test_regression_reverts_the_formatting(monkeypatch, tmp_path) -> None:
    """A behaviour change must restore the tree, not ship the reformatted one."""
    import stress_stack.hygiene_dispatcher as hd
    from stress_stack.hygiene_verify import AVAILABLE, SuiteSnapshot

    (tmp_path / "main.go").write_text("package m\n\nfunc A() int { return 1 }\n")
    (tmp_path / "go.mod").write_text("module m\n\ngo 1.22\n")

    reverted: list[bool] = []
    snapshots = iter(
        [
            SuiteSnapshot(AVAILABLE, 0, "ok"),        # before hygiene
            SuiteSnapshot(AVAILABLE, 1, "FAIL"),      # after hygiene
        ]
    )

    # The stage runs the command the workflow probed, so the test has to say what
    # that is rather than relying on a per-language branch that no longer exists.
    _write_workflow(tmp_path, "go", {"format": ["gofmt", "-l", "-w", "."]})
    monkeypatch.setattr(hd, "_changed_file_count", lambda *a, **k: 3)
    monkeypatch.setattr("stress_stack.hygiene_verify.build_probe_image", lambda *a: ("img", ""))
    monkeypatch.setattr("stress_stack.hygiene_verify.snapshot_suite", lambda *a: next(snapshots))
    monkeypatch.setattr(
        "stress_stack.hygiene_verify.revert_working_tree",
        lambda root: reverted.append(True) or True,
    )
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(hd, "run", lambda *a, **k: type("R", (), {"ok": True, "stdout": "", "stderr": "", "failure_detail": lambda s: ""})())
    monkeypatch.setattr(
        "stress_stack.linters.lint",
        lambda root, lang, command=None: __import__(
            "stress_stack.linters", fromlist=["LintOutcome"]
        ).LintOutcome(
            status="linted", tool="go vet", violations_before=0,
            violations_after=0, fixed=0, measured=True,
        ),
    )

    report = dispatch_hygiene(tmp_path, _profile("go"))

    assert reverted == [True]
    assert report.status == "reverted"
    assert report.regressions_verified is True
    assert report.files_reformatted == 0, "a reverted tree has no reformatted files"


# --- the repair agent is never believed without verification -----------------


def _lint_outcome(after: int, tool: str = "go vet"):
    from stress_stack.linters import LintOutcome

    return LintOutcome(
        status="linted", tool=tool, violations_before=after + 2,
        violations_after=after, fixed=2, measured=True, residual={"x": after},
    )


def test_repair_reverts_when_the_suite_output_changes(monkeypatch, tmp_path) -> None:
    """A fix that alters behaviour is discarded, however good it looks."""
    import stress_stack.lint_repair as lr

    (tmp_path / "a.go").write_text("package m\nfunc A() int { return 1 }\n")
    restored: list[bool] = []

    monkeypatch.setattr(lr, "_snapshot_tracked", lambda root: "diff")
    monkeypatch.setattr(lr, "_restore", lambda root: restored.append(True) or True)
    monkeypatch.setattr(lr, "_ask_for_edits", lambda *a, **k: [
        {"path": "a.go", "content": "package m\nfunc A() int { return 2 }\n", "rationale": "x"}
    ])
    monkeypatch.setattr("stress_stack.linters.lint", lambda root, language: _lint_outcome(1))
    monkeypatch.setattr("stress_stack.hygiene_verify.build_probe_image", lambda *a: ("img", ""))
    monkeypatch.setattr(lr, "_probe_profile", lambda root: None)

    from stress_stack.hygiene_verify import AVAILABLE, SuiteSnapshot

    # Violations fell, but the suite output moved — that is a reject.
    monkeypatch.setattr("stress_stack.linters.lint", lambda root, language: _lint_outcome(1))
    monkeypatch.setattr(
        "stress_stack.hygiene_verify.snapshot_suite",
        lambda img: SuiteSnapshot(AVAILABLE, 0, "DIFFERENT"),
    )

    result = lr.repair_lint_violations(
        tmp_path, "go", client=object(), probe_image="img", baseline_output="ORIGINAL"
    )

    assert restored == [True], "the tree must be restored"
    assert result.rounds[-1].accepted is False
    assert "suite output changed" in result.rounds[-1].reason


def test_repair_is_skipped_without_a_way_to_verify(tmp_path) -> None:
    """No probe image means no verification, so no edits are attempted."""
    from stress_stack.lint_repair import repair_lint_violations

    result = repair_lint_violations(
        tmp_path, "go", client=object(), probe_image=None, baseline_output=""
    )
    assert result.status == "skipped"
    assert "verify" in result.reason


def test_repair_rejects_edits_outside_the_repository(monkeypatch, tmp_path) -> None:
    """A path traversal in a model's answer must never be written."""
    import stress_stack.lint_repair as lr

    (tmp_path / "a.go").write_text("package m\n")
    outside = tmp_path.parent / "escaped.go"
    outside.write_text("original\n")

    monkeypatch.setattr(lr, "_snapshot_tracked", lambda root: "diff")
    monkeypatch.setattr(lr, "_restore", lambda root: True)
    monkeypatch.setattr(lr, "_probe_profile", lambda root: None)
    monkeypatch.setattr(lr, "_ask_for_edits", lambda *a, **k: [
        {"path": "../escaped.go", "content": "PWNED\n", "rationale": "x"}
    ])
    monkeypatch.setattr("stress_stack.linters.lint", lambda root, language: _lint_outcome(1))
    monkeypatch.setattr("stress_stack.hygiene_verify.build_probe_image", lambda *a: (None, "no"))
    monkeypatch.setattr("stress_stack.hygiene_verify.snapshot_suite", lambda img: None)

    lr.repair_lint_violations(
        tmp_path, "go", client=object(), probe_image="img", baseline_output=""
    )

    assert outside.read_text() == "original\n", "wrote outside the repository"


# --- pipeline 3: per-language test results feed the existing gates ------------


GO_JSON = (
    '{"Action":"run","Package":"ex/p","Test":"TestAdd"}\n'
    '{"Action":"output","Package":"ex/p","Test":"TestAdd","Output":"=== RUN   TestAdd\\n"}\n'
    '{"Action":"pass","Package":"ex/p","Test":"TestAdd"}\n'
    '{"Action":"run","Package":"ex/p","Test":"TestMul"}\n'
    '{"Action":"output","Package":"ex/p","Test":"TestMul","Output":"    calc_test.go:10: want 7, got 6\\n"}\n'
    '{"Action":"fail","Package":"ex/p","Test":"TestMul"}\n'
)


def test_go_results_become_a_run_report() -> None:
    from stress_stack.test_runners import plan_for

    report = plan_for("go").parse(GO_JSON, "", 1)

    assert report.collected is True
    assert report.passing() == {"ex/p::TestAdd"}
    assert report.failing() == {"ex/p::TestMul"}
    assert report.results["ex/p::TestMul"].failure_class == "assertion"


def test_go_build_failure_is_not_a_collected_run() -> None:
    """A package that does not compile can never satisfy a fail-before gate."""
    from stress_stack.test_runners import plan_for

    report = plan_for("go").parse("", "./calc.go:4:22: syntax error: unexpected ;", 1)

    assert report.collected is False
    assert report.results == {}


def test_existing_gates_accept_go_reports_unchanged() -> None:
    """The gates were never Python-specific — only RunReport production was."""
    from stress_stack.test_runners import plan_for
    from stress_stack.verification import gate_fail_before, gate_pass_after

    plan = plan_for("go")
    before = plan.parse(GO_JSON, "", 1)
    after = plan.parse(GO_JSON.replace('"Action":"fail"', '"Action":"pass"'), "", 0)
    targets = {"ex/p::TestMul"}

    assert gate_fail_before(before, targets).passed is True
    assert gate_pass_after(after, targets).passed is True


def test_go_build_failure_fails_the_fail_before_gate() -> None:
    """Collection failure must be rejected, not counted as a behavioural failure."""
    from stress_stack.test_runners import plan_for
    from stress_stack.verification import gate_fail_before

    report = plan_for("go").parse("", "build failed", 2)
    verdict = gate_fail_before(report, {"ex/p::TestMul"})

    assert verdict.passed is False
    assert verdict.reason_code == "collection_failed"


def test_rust_libtest_output_becomes_a_run_report() -> None:
    from stress_stack.test_runners import plan_for

    output = (
        "running 2 tests\n"
        "test tests::adds ... ok\n"
        "test tests::muls ... FAILED\n"
        "\nfailures:\n\n"
        "---- tests::muls stdout ----\n"
        "assertion `left == right` failed\n"
        "  left: 6\n right: 7\n"
        "\ntest result: FAILED. 1 passed; 1 failed\n"
    )
    report = plan_for("rust").parse(output, "", 101)

    assert report.collected is True
    assert report.passing() == {"tests::adds"}
    assert report.results["tests::muls"].failure_class == "assertion"


def test_unsupported_ecosystems_have_no_plan() -> None:
    """Absent beats half-supported: validate must report them, not guess."""
    from stress_stack.test_runners import plan_for

    assert plan_for("typescript") is None
    assert plan_for("cpp") is None


# --- pipeline 3: per-test coverage attribution -------------------------------


def test_go_test_ids_are_attributed_to_their_package(monkeypatch) -> None:
    """`go test -list` prints names *before* the package line that owns them.

    Attributing each name to the package seen so far produced ids like
    `::TestAdd`, which matched nothing in the run report and silently broke the
    link between coverage and validation targets.
    """
    import stress_stack.coverage_multilang as cml

    listing = "TestAdd\nTestMul\nok  \texample.com/gates\t0.002s\n"
    monkeypatch.setattr(
        cml, "run", lambda *a, **k: type("R", (), {"ok": True, "stdout": listing, "stderr": ""})()
    )

    assert cml._go_test_ids(Path("/nonexistent")) == [
        ("example.com/gates", "TestAdd"),
        ("example.com/gates", "TestMul"),
    ]


def test_go_coverage_profile_skips_unexecuted_blocks(tmp_path) -> None:
    """A block with zero hits is not coverage."""
    import stress_stack.coverage_multilang as cml

    (tmp_path / "go.mod").write_text("module example.com/m\n\ngo 1.22\n")
    profile = tmp_path / "p.out"
    profile.write_text(
        "mode: set\n"
        "example.com/m/calc.go:3.30,5.2 2 1\n"
        "example.com/m/calc.go:7.30,9.2 2 0\n"
    )

    covered = cml._parse_go_profile(tmp_path, profile)

    assert covered == {"calc.go": {3, 4, 5}}, "zero-hit block must not be counted"


def test_coverage_map_attributes_lines_to_the_owning_symbol() -> None:
    from stress_stack.coverage_multilang import AttributionResult, build_coverage_map
    from stress_stack.parsers.tree_sitter_core import parse_source_code

    source = (
        "package m\n"
        "\n"
        "func Add(a, b int) int {\n"
        "\treturn a + b\n"
        "}\n"
        "\n"
        "func Unused() int {\n"
        "\treturn 0\n"
        "}\n"
    )
    parsed = parse_source_code("calc.go", source)
    graph = type("G", (), {"files": [parsed]})()

    attribution = AttributionResult(
        status="available",
        lines={"calc.go": {4: ["p::TestAdd"]}},
        tests_measured=1,
        tests_total=1,
    )
    coverage = build_coverage_map(graph, attribution)

    assert coverage.status == "available"
    assert coverage.symbols["calc.go::Add"].covering_tests == ["p::TestAdd"]
    assert coverage.symbols["calc.go::Unused"].covering_tests == []
    assert coverage.symbols["calc.go::Unused"].covered_lines == 0


def test_coverage_is_unavailable_rather_than_empty_for_unsupported() -> None:
    """An ecosystem with no attributor must not look like a fully-uncovered one."""
    from stress_stack.coverage_multilang import measure

    result = measure("/nonexistent", "cpp", type("G", (), {"files": []})())

    assert result.status == "unavailable"
    assert "cpp" in (result.reason or "")


def test_runner_selection_refuses_an_unsupported_ecosystem() -> None:
    """No test plan means no gate verdicts, not verdicts from an exit code."""
    from stress_stack.errors import ToolingError
    from stress_stack.runner import select_runner

    try:
        select_runner(image="x:y", language="cpp")
    except ToolingError as exc:
        assert "no test plan" in str(exc).lower()
    else:
        raise AssertionError("expected ToolingError for an unsupported ecosystem")


def test_formatting_line_shifts_are_not_a_regression() -> None:
    """gofmt moves lines; a failure message's line number moving is not a change.

    Without this, a successful reformat reverts itself: the only difference
    between the before and after suite output is the line number the failure is
    reported at.
    """
    from stress_stack.hygiene_verify import AVAILABLE, SuiteSnapshot, _normalize, compare

    before = _normalize("--- FAIL: TestMul\n    calc_test.go:10: want 7, got 6\n")
    after = _normalize("--- FAIL: TestMul\n    calc_test.go:13: want 7, got 6\n")
    assert before == after

    regressed, _ = compare(
        SuiteSnapshot(AVAILABLE, 1, before), SuiteSnapshot(AVAILABLE, 1, after)
    )
    assert regressed is False


def test_a_real_message_change_is_still_a_regression() -> None:
    """Normalising line numbers must not blunt the check itself."""
    from stress_stack.hygiene_verify import AVAILABLE, SuiteSnapshot, _normalize, compare

    before = _normalize("    calc_test.go:10: want 7, got 6\n")
    after = _normalize("    calc_test.go:10: want 7, got 9\n")

    regressed, why = compare(
        SuiteSnapshot(AVAILABLE, 1, before), SuiteSnapshot(AVAILABLE, 1, after)
    )
    assert regressed is True and "output changed" in why


# --- bugs found by running pipeline 3 on a real Go repository ----------------


def test_single_line_function_excision_keeps_the_declaration() -> None:
    """Line-range excision deleted the signature of a one-line function.

    `func Mul(a, b int) int { return a * b }` has its body on the signature's
    line, so replacing that line range left a bare `panic(...)` at file scope
    and the package stopped compiling — which the gates then rejected as a
    build failure rather than a behavioural one.
    """
    code = "package m\n\nfunc Add(a, b int) int { return a + b }\nfunc Mul(a, b int) int { return a * b }\n"
    result = excise_symbol("calc.go", code, "Mul")

    assert result is not None
    assert "func Mul(a, b int) int {" in result.stubbed
    assert 'panic("not implemented")' in result.stubbed
    # The untouched neighbour must survive intact.
    assert "func Add(a, b int) int { return a + b }" in result.stubbed
    # And the excised body must actually be gone.
    assert "return a * b" not in result.stubbed


def test_single_line_rust_excision_keeps_the_declaration() -> None:
    result = excise_symbol("lib.rs", "pub fn add(a: i32) -> i32 { a + 1 }\n", "add")

    assert result is not None
    assert "pub fn add(a: i32) -> i32 {" in result.stubbed
    assert "todo!()" in result.stubbed
    assert "a + 1" not in result.stubbed


def test_test_designation_works_inside_the_metadata_directory() -> None:
    """Staged trees live under `.stress_stack`, so the filter must be relative.

    Testing the absolute path parts excluded every file in the staged tree and
    designated no verifier files at all, which failed `verifier_integrity` on
    tasks whose other seven gates had passed.
    """
    from stress_stack.tasks import _designate_tests

    with tempfile.TemporaryDirectory() as tmpdir:
        tree = Path(tmpdir) / ".stress_stack" / "tasks" / "t1" / "solution"
        tree.mkdir(parents=True)
        (tree / "calc.go").write_text("package m\n\nfunc Mul(a, b int) int { return a * b }\n")
        (tree / "calc_test.go").write_text(
            "package m\n\nimport \"testing\"\n\nfunc TestMul(t *testing.T) {}\n"
        )

        designated = _designate_tests(["example.com/m::TestMul"], tree, "calc.go")

    assert designated == {"calc_test.go": ["TestMul"]}


def test_compiled_languages_get_an_executable_tmpfs() -> None:
    """Go builds a test binary into /tmp and execs it; noexec blocks every test."""
    from stress_stack.sandbox import SandboxPolicy, build_arguments

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        blocked = build_arguments(
            "img", ["go", "test"], code_dir=root, evidence_dir=root,
            policy=SandboxPolicy(allow_tmp_exec=False),
        )
        allowed = build_arguments(
            "img", ["go", "test"], code_dir=root, evidence_dir=root,
            policy=SandboxPolicy(allow_tmp_exec=True),
        )

    assert not any("exec" in a for a in blocked if a.startswith("--tmpfs"))
    assert any(a.startswith("--tmpfs=/tmp:rw,exec") for a in allowed)
    # The relaxation must not weaken anything else.
    for arguments in (blocked, allowed):
        assert "--cap-drop=ALL" in arguments
        assert "--read-only" in arguments
        assert any(a.endswith(":/work:ro") for a in arguments)


def test_an_explicit_environment_suppresses_the_pythonpath_default() -> None:
    """`{}` is a decision, not an absence.

    LanguageRunner passes an empty mapping precisely to keep a Python variable
    out of a Go or Rust image. Merging the default in afterwards made that
    comment aspirational — harmless while those images have no interpreter, and
    silently wrong the moment one does.
    """
    from stress_stack.sandbox import SandboxPolicy, build_arguments

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        common = {
            "code_dir": root,
            "evidence_dir": root,
            "policy": SandboxPolicy(),
        }
        default = build_arguments("img", ["go", "test"], **common)
        suppressed = build_arguments("img", ["go", "test"], environment={}, **common)
        explicit = build_arguments(
            "img", ["python", "-m", "pytest"],
            environment={"PYTHONPATH": "/work/src:/work"}, **common,
        )

    assert "--env=PYTHONPATH=/work" in default
    assert not any(a.startswith("--env=PYTHONPATH") for a in suppressed)
    assert "--env=PYTHONPATH=/work/src:/work" in explicit
    # Suppressing PYTHONPATH must not suppress the sanitised environment.
    assert "--env=PYTHONHASHSEED=0" in suppressed


def test_hygiene_never_formats_the_staged_task_trees() -> None:
    """An excision task's input/ is deliberately unimplemented, not a lint target."""
    from stress_stack.hygiene_dispatcher import _owned_sources

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "calc.go").write_text("package m\n")
        staged = root / ".stress_stack" / "tasks" / "t1" / "input"
        staged.mkdir(parents=True)
        (staged / "calc.go").write_text("package m\n    panic(\"not implemented\")\n")

        found = [p.name for p in _owned_sources(root, (".go",))]

    assert found == ["calc.go"]
    assert len(found) == 1, "must not reach into .stress_stack"


# --------------------------------------------------------------------------
# Rust per-test coverage — the attributor that stops Rust being a dead end
# --------------------------------------------------------------------------


def test_rust_is_registered_as_an_attributor() -> None:
    """Without one, mine_excision yields nothing and the run stops with no reason."""
    from stress_stack.coverage_multilang import _ATTRIBUTORS

    assert set(_ATTRIBUTORS) >= {"go", "rust"}


def test_lcov_lines_become_repository_relative(tmp_path: Path) -> None:
    from stress_stack.coverage_multilang import _parse_lcov

    (tmp_path / "src").mkdir()
    report = tmp_path / "cov.lcov"
    report.write_text(
        f"SF:{tmp_path}/src/lib.rs\n"
        "DA:1,3\nDA:2,0\nDA:7,1\n"
        "end_of_record\n",
        encoding="utf-8",
    )

    assert _parse_lcov(tmp_path, report) == {"src/lib.rs": {1, 7}}


def test_a_dependency_compiled_outside_the_workspace_is_dropped(tmp_path: Path) -> None:
    """It cannot be attributed to a symbol in this graph, so it is not kept."""
    from stress_stack.coverage_multilang import _parse_lcov

    report = tmp_path / "cov.lcov"
    report.write_text(
        "SF:/root/.cargo/registry/src/serde/lib.rs\nDA:5,9\nend_of_record\n"
        f"SF:{tmp_path}/src/lib.rs\nDA:3,1\nend_of_record\n",
        encoding="utf-8",
    )

    assert _parse_lcov(tmp_path, report) == {"src/lib.rs": {3}}


def test_an_unexecuted_line_is_not_coverage(tmp_path: Path) -> None:
    from stress_stack.coverage_multilang import _parse_lcov

    report = tmp_path / "cov.lcov"
    report.write_text(f"SF:{tmp_path}/a.rs\nDA:1,0\nDA:2,0\nend_of_record\n", encoding="utf-8")

    assert _parse_lcov(tmp_path, report) == {"a.rs": set()}


def test_a_missing_report_is_empty_rather_than_an_error(tmp_path: Path) -> None:
    from stress_stack.coverage_multilang import _parse_lcov

    assert _parse_lcov(tmp_path, tmp_path / "absent.lcov") == {}


def test_rust_test_ids_come_from_the_terse_listing(tmp_path: Path, monkeypatch) -> None:
    from stress_stack import coverage_multilang as cm

    class _Result:
        ok = True
        stdout = (
            "\nrunning 3 tests\n"
            "tests::adds_two: test\n"
            "tests::handles_zero: test\n"
            "benches::speed: bench\n"
            "\n3 tests, 1 benchmark\n"
        )
        stderr = ""

    monkeypatch.setattr(cm, "run", lambda *a, **k: _Result())

    assert cm._rust_test_ids(tmp_path) == [("", "tests::adds_two"), ("", "tests::handles_zero")]


def test_a_missing_llvm_cov_is_reported_not_worked_around(tmp_path: Path, monkeypatch) -> None:
    """An empty map that claims to be available is how mining gets nothing and no reason."""
    from stress_stack import coverage_multilang as cm

    class _Absent:
        ok = False
        stdout = ""
        stderr = "command not found"

    monkeypatch.setattr(cm, "run", lambda *a, **k: _Absent())
    result = cm.attribute_rust(tmp_path)

    assert result.status == "unavailable"
    assert "cargo_llvm_cov_not_installed" in result.reason
    assert "cargo install cargo-llvm-cov" in result.reason


def test_go_images_can_run_the_race_detector() -> None:
    """`go test -race` needs cgo, and many Go projects' CI runs it.

    The detector prefers a project's own CI command over the default, so an
    Alpine base — no C toolchain, cgo off — turned spf13/cast's declared command
    into `-race requires cgo` and failed the container stage.
    """
    from stress_stack.container_doctor import synthesize_dockerfile

    profile = _profile("go", base_image="golang:1.22-bookworm")
    dockerfile = synthesize_dockerfile(profile, "golang:1.22-bookworm")

    assert "alpine" not in dockerfile.lower()
    assert "ENV CGO_ENABLED=1" in dockerfile


def test_the_detected_go_base_is_not_alpine() -> None:
    from stress_stack.project_detector import detect_project_profile
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "go.mod").write_text("module example.com/m\n\ngo 1.22\n", encoding="utf-8")
        profile = detect_project_profile(root)

    assert profile.primary_language == "go"
    assert "alpine" not in profile.base_image


def test_parallel_test_interleaving_is_not_a_regression() -> None:
    """Two runs of an unchanged suite must compare equal.

    Go's `-v` output orders lines by goroutine scheduling, so a suite using
    t.Parallel() emits the same lines in a different sequence every run.
    Measured on spf13/cast: 32233 identical lines, different order, and hygiene
    reverted its own formatting and failed the pipeline over it.
    """
    from stress_stack.hygiene_verify import AVAILABLE, SuiteSnapshot, compare

    first = (
        "=== RUN   TestBool/#01\n=== PAUSE TestBool/#01\n"
        "=== CONT  TestBool/#57\n--- PASS: TestBool (0.00s)\nPASS\n"
    )
    shuffled = (
        "=== CONT  TestBool/#57\n=== RUN   TestBool/#01\n"
        "--- PASS: TestBool (0.00s)\n=== PAUSE TestBool/#01\nPASS\n"
    )

    def snap(text: str) -> SuiteSnapshot:
        from stress_stack.hygiene_verify import _normalize

        return SuiteSnapshot(status=AVAILABLE, exit_code=0, normalized=_normalize(text))

    regressed, reason = compare(snap(first), snap(shuffled))

    assert not regressed, reason


def test_a_test_that_starts_failing_is_still_a_regression() -> None:
    """The exemption is for order alone; content must still be compared."""
    from stress_stack.hygiene_verify import AVAILABLE, SuiteSnapshot, _normalize, compare

    def snap(text: str, exit_code: int = 0) -> SuiteSnapshot:
        return SuiteSnapshot(status=AVAILABLE, exit_code=exit_code, normalized=_normalize(text))

    passing = snap("--- PASS: TestBool (0.00s)\n--- PASS: TestInt (0.00s)\nPASS\n")
    failing = snap("--- PASS: TestBool (0.00s)\n--- FAIL: TestInt (0.00s)\nFAIL\n")

    regressed, _ = compare(passing, failing)
    assert regressed


def test_a_disappearing_test_is_still_a_regression() -> None:
    from stress_stack.hygiene_verify import AVAILABLE, SuiteSnapshot, _normalize, compare

    def snap(text: str) -> SuiteSnapshot:
        return SuiteSnapshot(status=AVAILABLE, exit_code=0, normalized=_normalize(text))

    before = snap("--- PASS: TestBool (0.00s)\n--- PASS: TestInt (0.00s)\nPASS\n")
    after = snap("--- PASS: TestBool (0.00s)\nPASS\n")

    regressed, _ = compare(before, after)
    assert regressed


def test_two_container_runs_are_not_nondeterministic_from_scheduling() -> None:
    """The determinism gate must answer about the code, not the goroutine order.

    This is the same defect as the hygiene comparison, in a second copy of the
    same helper: spf13/cast passed hygiene and then failed the container stage
    for the identical reason one stage later.
    """
    from stress_stack.container_doctor import _normalize

    first = "=== RUN   TestA\n=== PAUSE TestA\n=== CONT  TestB\n--- PASS: TestA (0.01s)\nPASS"
    shuffled = "=== CONT  TestB\n--- PASS: TestA (0.02s)\n=== RUN   TestA\n=== PAUSE TestA\nPASS"

    assert _normalize(first) == _normalize(shuffled)


def test_a_container_run_that_starts_failing_is_still_caught() -> None:
    from stress_stack.container_doctor import _normalize

    passing = "--- PASS: TestA (0.01s)\n--- PASS: TestB (0.01s)\nPASS"
    failing = "--- PASS: TestA (0.01s)\n--- FAIL: TestB (0.01s)\nFAIL"

    assert _normalize(passing) != _normalize(failing)


def _write_workflow(root, language: str, commands: dict) -> None:
    """A resolved workflow, as the workflow stage would have left it."""
    import json

    from stress_stack.workflow import HYGIENE, CapabilityRecord, Probe, Workflow

    stamp = root / ".stress_stack" / "workflow.json"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    workflow = Workflow(
        language=language,
        capabilities={
            HYGIENE: CapabilityRecord(
                name=HYGIENE, source="default", commands=commands, probe=Probe(True)
            )
        },
    )
    stamp.write_text(json.dumps(workflow.to_dict()), encoding="utf-8")
