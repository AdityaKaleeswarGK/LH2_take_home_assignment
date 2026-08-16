"""What the two full runs are allowed to conclude.

The screen is where a candidate becomes a task or stops being one, and it is
the piece that replaced every static guess about which tests "belong" to a
change. It reaches its verdict from two run reports and nothing else, so it can
be tested exhaustively without a container.
"""

from __future__ import annotations

from stress_stack.runner import RunOutcome
from stress_stack.tasks import MEASURED, STRICT, screen_transition
from stress_stack.verification import (
    ASSERTION,
    BEHAVIORAL_EXCEPTION,
    INFRASTRUCTURE,
    PASSED,
    CaseResult,
    RunReport,
)


def outcome(results: dict[str, tuple[str, str]], name: str = "run") -> RunOutcome:
    """A run report built from {test_id: (status, failure_class)}."""
    report = RunReport()
    for test_id, (status, failure_class) in results.items():
        report.results[test_id] = CaseResult(test_id, status, failure_class, "sig")
    return RunOutcome(
        name=name,
        report=report,
        exit_code=0,
        seconds=0.1,
        backend="docker",
        infrastructure_failure=None,
    )


def passing(*test_ids: str) -> dict[str, tuple[str, str]]:
    return {test_id: (PASSED, PASSED) for test_id in test_ids}


def test_a_test_that_ran_and_failed_on_an_assertion_is_designated() -> None:
    before = outcome({"m::a": ("failed", ASSERTION), **passing("m::b")})
    after = outcome(passing("m::a", "m::b"))

    screen = screen_transition(before, after)

    assert screen.ran_and_failed == ["m::a"]
    assert screen.passing_both == 1
    assert screen.designated(STRICT) == ["m::a"]


def test_an_exception_from_repository_code_also_counts() -> None:
    """An absent feature usually raises rather than tripping an assert."""
    before = outcome({"m::a": ("failed", BEHAVIORAL_EXCEPTION)})
    after = outcome(passing("m::a"))

    assert screen_transition(before, after).designated(STRICT) == ["m::a"]


def test_a_test_that_could_not_be_collected_is_kept_apart() -> None:
    """The disputed class: the verifier never loaded, so no test body ran.

    It is recorded rather than silently counted or silently dropped, because
    whether it satisfies "fail-before for the right reason" is a reading of the
    brief and not something this function should decide.
    """
    before = outcome(passing("m::b"))
    after = outcome(passing("m::a", "m::b"))

    screen = screen_transition(before, after)

    assert screen.absent_before == ["m::a"]
    assert screen.ran_and_failed == []
    assert screen.designated(STRICT) == []
    assert screen.designated(MEASURED) == ["m::a"]


def test_the_two_policies_differ_only_on_that_class() -> None:
    before = outcome({"m::a": ("failed", ASSERTION), **passing("m::c")})
    after = outcome(passing("m::a", "m::b", "m::c"))

    screen = screen_transition(before, after)

    assert screen.designated(STRICT) == ["m::a"]
    assert screen.designated(MEASURED) == ["m::a", "m::b"]


def test_a_load_failure_on_a_collected_test_is_never_designated() -> None:
    """An import error inside a test that pytest did collect is infrastructure."""
    before = outcome({"m::a": ("failed", INFRASTRUCTURE)})
    after = outcome(passing("m::a"))

    screen = screen_transition(before, after)

    assert screen.failed_on_load == ["m::a"]
    assert screen.designated(STRICT) == []
    assert screen.designated(MEASURED) == []


def test_a_sweep_that_changes_no_verdict_is_rejected() -> None:
    """glom's Python 3.12 pull request: two dozen tests touched, none affected."""
    both = passing(*[f"m::t{index}" for index in range(24)])
    screen = screen_transition(outcome(both), outcome(both))

    assert screen.designated(STRICT) == []
    assert screen.designated(MEASURED) == []
    assert screen.passing_both == 24
    assert screen.rejection(STRICT) == "no_test_changed_verdict"


def test_the_rejection_names_the_uncollectable_case_specifically() -> None:
    """So the funnel distinguishes "nothing changed" from "policy excluded it"."""
    before = outcome(passing("m::b"))
    after = outcome(passing("m::a", "m::b"))

    screen = screen_transition(before, after)

    assert screen.rejection(STRICT) == "only_uncollectable_tests_changed_verdict"


def test_a_fix_for_an_untouched_failing_test_is_designated() -> None:
    """The case a static file-diff filter cannot see at all.

    The test was committed with the bug it pins and fails until the fix lands.
    The change touches no test file, so nothing in the diff points at it.
    """
    before = outcome({"m::test_render_escapes": ("failed", ASSERTION)})
    after = outcome(passing("m::test_render_escapes"))

    assert screen_transition(before, after).designated(STRICT) == ["m::test_render_escapes"]


def test_a_test_removed_by_the_change_is_not_designated() -> None:
    """Absent *after* is a deletion, not a verdict change."""
    before = outcome(passing("m::gone", "m::kept"))
    after = outcome(passing("m::kept"))

    screen = screen_transition(before, after)

    assert screen.designated(MEASURED) == []
    assert screen.passing_both == 1


def test_collection_errors_are_reported_for_auditing() -> None:
    before = outcome({"m::broken": ("failed", INFRASTRUCTURE), **passing("m::ok")})
    after = outcome(passing("m::ok"))

    assert screen_transition(before, after).to_dict()["screen"]["collection_errors"] == [
        "m::broken"
    ]
