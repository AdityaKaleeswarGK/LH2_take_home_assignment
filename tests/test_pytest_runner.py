from __future__ import annotations

from pathlib import Path

from stress_stack.pytest_runner import PytestRunResult, _parse_report, compare

_REPORT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="1" failures="1" skipped="1" tests="4">
<testcase classname="tests.test_a" name="test_ok" time="0.01" />
<testcase classname="tests.test_a" name="test_bad" time="0.01"><failure message="boom">t</failure></testcase>
<testcase classname="tests.test_b" name="test_broken" time="0.01"><error message="err">t</error></testcase>
<testcase classname="tests.test_b" name="test_skip" time="0.01"><skipped type="pytest.skip">s</skipped></testcase>
</testsuite></testsuites>
"""


def result(outcomes: dict[str, str]) -> PytestRunResult:
    return PytestRunResult(
        status="ran",
        reason=None,
        outcomes=outcomes,
        exit_code=0,
        duration_seconds=0.1,
    )


def test_parses_every_junit_outcome(tmp_path: Path) -> None:
    path = tmp_path / "report.xml"
    path.write_text(_REPORT, encoding="utf-8")

    outcomes = _parse_report(path)

    assert outcomes == {
        "tests.test_a::test_ok": "passed",
        "tests.test_a::test_bad": "failed",
        "tests.test_b::test_broken": "error",
        "tests.test_b::test_skip": "skipped",
    }


def test_missing_or_invalid_report_is_empty(tmp_path: Path) -> None:
    assert _parse_report(tmp_path / "absent.xml") == {}
    broken = tmp_path / "broken.xml"
    broken.write_text("<testsuite>", encoding="utf-8")
    assert _parse_report(broken) == {}


def test_counts_and_passing_set() -> None:
    run = result({"a": "passed", "b": "failed", "c": "passed", "d": "skipped"})
    assert run.passing == {"a", "c"}
    assert run.counts() == {"passed": 2, "failed": 1, "skipped": 1}


def test_compare_detects_regression_not_preexisting_failure() -> None:
    before = result({"a": "passed", "b": "failed", "c": "passed"})
    after = result({"a": "passed", "b": "failed", "c": "failed"})

    outcome = compare(before, after)

    assert outcome["regressions"] == ["c"]
    assert outcome["repairs"] == []


def test_compare_reports_repairs_and_membership_changes() -> None:
    before = result({"a": "failed", "gone": "passed"})
    after = result({"a": "passed", "new": "passed"})

    outcome = compare(before, after)

    assert outcome["repairs"] == ["a", "new"]
    assert outcome["regressions"] == ["gone"]
    assert outcome["disappeared"] == ["gone"]
    assert outcome["appeared"] == ["new"]
