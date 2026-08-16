from __future__ import annotations

from pathlib import Path

from stress_stack.verification import (
    ASSERTION,
    BEHAVIORAL_EXCEPTION,
    INFRASTRUCTURE,
    PASSED,
    RunReport,
    CaseResult,
    classify,
    gate_collateral,
    gate_determinism,
    gate_fail_before,
    gate_pass_after,
    gate_verifier_integrity,
    normalize_signature,
    parse_report,
    summarize,
)

_REPORT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="4">
<testcase classname="t.test_a" name="ok" time="0.01" />
<testcase classname="t.test_a" name="asserts"><failure message="AssertionError: behavioral claim
assert (1 + 1) == 3">x</failure></testcase>
<testcase classname="t.test_a" name="types"><failure message="KeyError: missing">glom/test/test_x.py:85: in test_types
    assert repr(spec) == repr(rt)
glom/core.py:722: in __repr__
    return _format_t(_T_PATHS[self][1:])
E   KeyError: missing</failure></testcase>
<testcase classname="t.test_a" name="external"><failure message="TypeError: bad">/usr/lib/python3.12/json.py:41: in dumps
    return encoder.encode(o)
E   TypeError: bad</failure></testcase>
<testcase classname="t.test_b" name="fixture"><error message="failed on setup with RuntimeError">x</error></testcase>
</testsuite></testsuites>
"""

_COLLECTION_FAILURE = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="1">
<testcase classname="" name="test_mod"><error message="collection failure">ImportError</error></testcase>
</testsuite></testsuites>
"""


def report_from(entries: dict[str, tuple[str, str, str]]) -> RunReport:
    report = RunReport()
    for test_id, (status, failure_class, signature) in entries.items():
        report.results[test_id] = CaseResult(test_id, status, failure_class, signature)
    return report


_REPO_TRACE = "glom/core.py:722: in __repr__\n    return _format_t(x)\nE   KeyError: k"
_EXTERNAL_TRACE = "/usr/lib/python3.12/weakref.py:415: in __getitem__\nE   KeyError: k"


def test_classifies_by_failure_origin_not_exception_name() -> None:
    """An absent feature usually raises from library code rather than asserting."""
    assert classify("failure", "AssertionError: nope") == ASSERTION
    assert classify("failure", "KeyError: k", _REPO_TRACE) == BEHAVIORAL_EXCEPTION
    assert classify("failure", "KeyError: k", _EXTERNAL_TRACE) == INFRASTRUCTURE
    assert classify("failure", "ImportError: no module", _REPO_TRACE) == INFRASTRUCTURE
    assert classify("failure", "SyntaxError: invalid", _REPO_TRACE) == INFRASTRUCTURE
    assert classify("error", "AssertionError: even this") == INFRASTRUCTURE


def test_the_real_glom_pickle_case_qualifies() -> None:
    """PR #49 fails with KeyError raised through glom/core.py — behavioural."""
    trace = (
        "glom/test/test_path_and_t.py:85: in test_t_picklability\n"
        "    assert repr(spec) == repr(rt_spec)\n"
        "glom/core.py:722: in __repr__\n"
        "    return _format_t(_T_PATHS[self][1:])\n"
        "/Users/x/miniconda3/lib/python3.12/weakref.py:415: in __getitem__\n"
        "E   KeyError: <weakref at 0x104102 to '_TType'>"
    )
    assert classify("failure", "KeyError: <weakref>", trace) == BEHAVIORAL_EXCEPTION


def test_signature_is_stable_across_paths_and_addresses() -> None:
    a = normalize_signature("AssertionError: at /home/a/repo/x.py line 12 obj 0x7ff1")
    b = normalize_signature("AssertionError: at /tmp/build/repo/x.py line 88 obj 0x2ab9")
    assert a == b


def test_parses_each_failure_class_from_junit(tmp_path: Path) -> None:
    path = tmp_path / "r.xml"
    path.write_text(_REPORT, encoding="utf-8")

    report = parse_report(path)

    assert report.collected is True
    assert report.results["t.test_a::asserts"].failure_class == ASSERTION
    assert report.results["t.test_a::types"].failure_class == BEHAVIORAL_EXCEPTION
    assert report.results["t.test_b::fixture"].failure_class == INFRASTRUCTURE
    assert report.results["t.test_a::external"].failure_class == INFRASTRUCTURE
    assert report.passing() == {"t.test_a::ok"}


def test_collection_failure_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "r.xml"
    path.write_text(_COLLECTION_FAILURE, encoding="utf-8")

    assert parse_report(path).collected is False
    assert parse_report(tmp_path / "absent.xml").collected is False


def test_fail_before_requires_the_designated_tests_to_fail() -> None:
    report = report_from(
        {
            "t::target": ("failed", ASSERTION, "AssertionError: x"),
            "t::other": ("failed", ASSERTION, "AssertionError: y"),
        }
    )
    assert gate_fail_before(report, {"t::target"}).passed is True

    passing = report_from({"t::target": ("passed", "passed", "")})
    verdict = gate_fail_before(passing, {"t::target"})
    assert verdict.passed is False
    assert verdict.reason_code == "targets_did_not_fail"


def test_fail_before_rejects_import_and_setup_failures() -> None:
    """The brief: an import error does not count as failing for the right reason."""
    report = report_from({"t::target": ("failed", INFRASTRUCTURE, "ImportError: no module")})
    verdict = gate_fail_before(report, {"t::target"})

    assert verdict.passed is False
    assert verdict.reason_code == "failed_for_wrong_reason"


def test_fail_before_rejects_a_missing_target() -> None:
    """A collection error hides the targets entirely, so absence is a rejection."""
    report = report_from({"t::unrelated": ("failed", ASSERTION, "AssertionError: x")})
    verdict = gate_fail_before(report, {"t::target"})

    assert verdict.passed is False
    assert verdict.reason_code == "targets_not_collected"
    assert verdict.detail["missing"] == ["t::target"]


def test_fail_before_accepts_repo_raised_exceptions_by_default() -> None:
    """The brief excludes load failures, not every non-assertion failure."""
    report = report_from({"t::target": ("failed", BEHAVIORAL_EXCEPTION, "KeyError: x")})

    assert gate_fail_before(report, {"t::target"}).passed is True
    assert gate_fail_before(report, {"t::target"}, require_assertion=True).passed is False


def test_pass_after_requires_every_target_green() -> None:
    green = report_from({"t::target": ("passed", "passed", "")})
    assert gate_pass_after(green, {"t::target"}).passed is True

    red = report_from({"t::target": ("failed", ASSERTION, "AssertionError")})
    assert gate_pass_after(red, {"t::target"}).reason_code == "targets_did_not_pass"


def test_collateral_compares_against_the_snapshot_baseline() -> None:
    baseline = report_from(
        {
            "a": ("passed", "passed", ""),
            "b": ("passed", "passed", ""),
            "c": ("failed", ASSERTION, "pre-existing"),
        }
    )
    good = report_from(
        {
            "a": ("passed", "passed", ""),
            "b": ("passed", "passed", ""),
            "c": ("failed", ASSERTION, "pre-existing"),
        }
    )
    assert gate_collateral(baseline, good).passed is True

    broken = report_from(
        {
            "a": ("passed", "passed", ""),
            "b": ("failed", ASSERTION, "new"),
            "c": ("failed", ASSERTION, "pre-existing"),
        }
    )
    verdict = gate_collateral(baseline, broken)
    assert verdict.passed is False
    assert verdict.detail["regressions"] == ["b"]


def test_collateral_rejects_collection_loss_and_new_infrastructure() -> None:
    baseline = report_from({"a": ("passed", PASSED, ""), "b": ("failed", ASSERTION, "old")})
    disappeared = report_from({"a": ("passed", PASSED, "")})
    assert gate_collateral(baseline, disappeared).reason_code == "tests_disappeared"

    broken = report_from(
        {
            "a": ("passed", PASSED, ""),
            "b": ("failed", INFRASTRUCTURE, "ImportError"),
        }
    )
    assert gate_collateral(baseline, broken).reason_code == "new_infrastructure_failures"


def test_determinism_catches_unstable_collection_status_and_signature() -> None:
    stable = report_from({"t::x": ("failed", ASSERTION, "AssertionError: a")})
    assert gate_determinism([stable, stable, stable], {"t::x"}).passed is True

    flaky_status = report_from({"t::x": ("passed", "passed", "")})
    assert (
        gate_determinism([stable, flaky_status], {"t::x"}).reason_code == "unstable_status"
    )

    flaky_signature = report_from({"t::x": ("failed", ASSERTION, "AssertionError: b")})
    assert (
        gate_determinism([stable, flaky_signature], {"t::x"}).reason_code
        == "unstable_signature"
    )

    missing = report_from({"t::y": ("failed", ASSERTION, "AssertionError: a")})
    assert gate_determinism([stable, missing], {"t::x"}).reason_code == "unstable_collection"

    assert gate_determinism([stable], {"t::x"}).reason_code == "insufficient_repeats"


def test_verifier_integrity_rejects_tests_that_read_the_answer() -> None:
    clean = {"tests/test_a.py": "def test_x():\n    assert compute(2) == 4\n"}
    assert gate_verifier_integrity(clean).passed is True

    cheating = {
        "tests/test_a.py": "import subprocess\n"
        "def test_x():\n"
        "    subprocess.run(['git', 'show', 'HEAD'])\n"
    }
    verdict = gate_verifier_integrity(cheating)
    assert verdict.passed is False
    assert "reads_git_history" in verdict.detail


def test_an_empty_verifier_is_not_valid() -> None:
    verdict = gate_verifier_integrity({})
    assert verdict.passed is False
    assert verdict.reason_code == "no_verifier_files"


def test_summary_lists_every_failed_gate() -> None:
    report = report_from({"t::target": ("failed", INFRASTRUCTURE, "ImportError")})
    verdicts = [
        gate_fail_before(report, {"t::target"}),
        gate_pass_after(report_from({"t::target": ("passed", "passed", "")}), {"t::target"}),
    ]
    summary = summarize(verdicts)

    assert summary["all_gates_passed"] is False
    assert summary["failed_gates"] == ["fail_before"]
    assert summary["reason_codes"] == ["failed_for_wrong_reason"]


def test_stale_bytecode_paths_do_not_defeat_origin_detection() -> None:
    """A .pyc compiled elsewhere makes frames absolute and foreign-looking."""
    stale = (
        "/build/host/tasks/t1/solution/glom/core.py:722: in __repr__\n"
        "    return _format_t(x)\n"
        "E   KeyError: k"
    )
    assert classify("failure", "KeyError: k", stale, code_root="/build/host/tasks/t1") == (
        BEHAVIORAL_EXCEPTION
    )
    site = (
        "/usr/local/lib/python3.12/site-packages/attr/_make.py:41: in x\nE   KeyError: k"
    )
    assert classify("failure", "KeyError: k", site, code_root="/build/host/tasks/t1") == (
        INFRASTRUCTURE
    )


def test_rewritten_assertions_without_the_prefix_are_assertions() -> None:
    """pytest's terse output drops `AssertionError:` for rewritten asserts."""
    message = 'assert "PathAccessEr...b\'), 0)" == "PathAccessEr...b\'), 0)"'
    trace = "glom/test/test_x.py:56: in test_msg\n    ???\nE   " + message

    assert classify("failure", message, trace) == ASSERTION
