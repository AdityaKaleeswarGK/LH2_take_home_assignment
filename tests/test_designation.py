"""Designation has to survive the narrowing that happens after it.

A designated test is chosen from two unfiltered runs and then re-run with the
suite narrowed to it. Go names a table-driven subtest `TestUintSlice/#06/...`,
and `go test -run` cannot select an auto-generated `#06` segment — so the
narrowed run collected nothing and the gate correctly reported
`targets_not_collected` for twelve of thirteen Go candidates.

These pin the fix from both directions: Go lifts to the ancestor the run
actually collected, and pytest does not lift at all.
"""

from __future__ import annotations

from stress_stack.test_runners import GoTestPlan, RustTestPlan
from stress_stack.verification import (
    CaseResult,
    PASSED,
    RunReport,
    nearest_collected_ancestor,
    resolve_targets,
)


def report(*test_ids: str) -> RunReport:
    run = RunReport()
    for test_id in test_ids:
        run.results[test_id] = CaseResult(test_id, PASSED, PASSED, "")
    return run


def test_a_go_subtest_absent_from_the_excised_run_lifts_to_its_parent() -> None:
    """The measured case: excision panics the parent, so later cases never exist.

    Reproduced against a real four-level table — the excised tree collects 4 ids
    where the reference tree collects 25.
    """
    excised = report(
        "m::TestUintSlice",
        "m::TestUintSlice/#00",
        "m::TestUintSlice/#00/Value",
        "m::TestUintSlice/#00/Value/ToType",
    )
    reference = report(
        "m::TestUintSlice",
        *[f"m::TestUintSlice/#{n:02d}/Value/ToType" for n in range(8)],
    )

    targets, record = resolve_targets(
        ["m::TestUintSlice/#06/Value/ToType"],
        [excised, reference],
        selection_id=GoTestPlan().selection_id,
    )

    assert targets == ["m::TestUintSlice"]
    assert record["lifted"] == {"m::TestUintSlice/#06/Value/ToType": "m::TestUintSlice"}
    assert record["unresolved"] == []


def test_a_target_present_in_only_one_run_does_not_count_as_collected() -> None:
    """The union would accept it on the strength of the run never in question."""
    before = report("m::TestX")
    after = report("m::TestX", "m::TestX/case")

    targets, record = resolve_targets(["m::TestX/case"], [before, after])

    assert targets == ["m::TestX"]
    assert record["lifted"] == {"m::TestX/case": "m::TestX"}


def test_a_pytest_parametrised_case_is_its_own_answer() -> None:
    """The parametrised case is the meaningful unit and must not be lifted."""
    collected = report(
        "test_x.py::TestSpec::test_call[a-1]",
        "test_x.py::TestSpec::test_call[b-2]",
    )

    targets, record = resolve_targets(
        ["test_x.py::TestSpec::test_call[a-1]"], [collected], selection_id=lambda t: t
    )

    assert targets == ["test_x.py::TestSpec::test_call[a-1]"]
    assert record["lifted"] == {}


def test_a_target_no_run_collected_is_left_alone_and_named() -> None:
    """An unresolvable target must still reach the gate and be rejected there."""
    targets, record = resolve_targets(
        ["pkg::TestGone"], [report("pkg::TestOther")], selection_id=lambda t: t
    )

    assert targets == ["pkg::TestGone"]
    assert record["unresolved"] == ["pkg::TestGone"]


def test_two_leaves_collapsing_onto_one_ancestor_is_recorded() -> None:
    """Weaker evidence than a leaf verdict, so the artifact has to say so."""
    collected = report("m::TestTable")

    targets, record = resolve_targets(
        ["m::TestTable/#00/case", "m::TestTable/#01/case"],
        [collected],
        selection_id=GoTestPlan().selection_id,
    )

    assert targets == ["m::TestTable"]
    assert record["collapsed"] == ["m::TestTable"]


def test_a_chain_no_run_shares_is_left_for_the_gate_to_reject() -> None:
    """A test only the reference run has is not evidence of anything."""
    before = report("m::TestKept")
    after = report("m::TestKept", "m::TestNew", "m::TestNew/sub")

    targets, record = resolve_targets(["m::TestNew/sub"], [before, after])

    assert targets == ["m::TestNew/sub"]
    assert record["unresolved"] == ["m::TestNew/sub"]


def test_nearest_ancestor_walks_both_separators() -> None:
    assert nearest_collected_ancestor("a/b/c", {"a/b"}) == "a/b"
    assert nearest_collected_ancestor("a/b/c", {"a"}) == "a"
    assert nearest_collected_ancestor("f.py::C::t", {"f.py::C"}) == "f.py::C"
    assert nearest_collected_ancestor("solo", {"other"}) is None


def test_go_selects_a_subtest_path_element_by_element() -> None:
    """`^TestX` unanchored at the end would also select `TestXExtra`."""
    command = GoTestPlan().suite_command(["m::TestX/named_case"])

    assert "-run" in command
    assert command[command.index("-run") + 1] == "^TestX$/^named_case$"


def test_rust_reports_ids_it_can_select_back() -> None:
    assert RustTestPlan().selection_id("mod::tests::works") == "mod::tests::works"
