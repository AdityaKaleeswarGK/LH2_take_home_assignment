"""Whether mining survives a repository shaped unlike the sample.

Every assertion here is about generality rather than correctness on glom. A
filter that is really an opinion shows up as an empty pool the moment the
repository stops sharing glom's habits, and that is only visible against a
repository built not to share them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from adversarial import build_adversarial_repository
from stress_stack.candidates import mine_history, module_index
from stress_stack.git_repository import GitRepository
from stress_stack.graph import build_graph


@pytest.fixture
def adversarial(tmp_path: Path) -> Path:
    build_adversarial_repository(tmp_path / "widget")
    return tmp_path / "widget"


def mined(root: Path):
    repository = GitRepository.discover(root)
    graph = build_graph(root)
    return mine_history(repository, graph, root / ".stress_stack" / "history")


def subjects(candidates) -> set[str]:
    return {candidate.subject for candidate in candidates}


def test_src_layout_modules_are_named_without_the_prefix(adversarial: Path) -> None:
    resolver = module_index(build_graph(adversarial))

    assert resolver.of("src/widget/core.py") == "widget.core"
    assert resolver.of("src/widget/removed.py") == "widget.removed"


def test_a_fix_for_an_already_failing_test_is_not_discarded(adversarial: Path) -> None:
    """PR#2 changes no test file, yet the repository already pins its bug.

    The test that proves the fix landed was committed with the bug and fails
    until the fix arrives. Requiring the change to touch a test file throws away
    a task whose fail-before evidence is stronger than most.
    """
    candidates, funnel, _ = mined(adversarial)

    assert "PR#2" not in funnel.dropped.get("no_test_change", []), (
        "a bug fix against a pre-existing failing test was dropped for touching no test"
    )
    assert "PR#2" in subjects(candidates)


def test_real_code_under_docs_is_not_treated_as_documentation(adversarial: Path) -> None:
    """PR#4 changes docs/generate.py, which is imported and tested."""
    candidates, funnel, _ = mined(adversarial)

    assert "PR#4" not in funnel.dropped.get("no_source_change", []), (
        "a source change was discarded because of where the file happens to live"
    )
    assert "PR#4" in subjects(candidates)


def test_prose_only_changes_still_leave_the_pool(adversarial: Path) -> None:
    """PR#3 touches only README.md and cannot produce a failing test."""
    candidates, _, _ = mined(adversarial)

    assert "PR#3" not in subjects(candidates)


def test_the_ordinary_feature_survives(adversarial: Path) -> None:
    candidates, _, _ = mined(adversarial)

    assert "PR#1" in subjects(candidates)


def test_nothing_is_dropped_for_a_reason_that_is_only_an_opinion(adversarial: Path) -> None:
    """Mining may reject only what cannot become a task at all.

    Structural impossibility is a fact: an unmerged pull request has no answer,
    a root commit has no parent, an empty diff has no change. Everything else is
    a prediction about quality, and predictions belong in the ranking where
    being wrong costs a position rather than a candidate.
    """
    _, funnel, _ = mined(adversarial)

    # No commit to materialise, no parent to diff against, and no Python for a
    # golden answer to contain. Each is a fact about the change, checkable
    # without predicting anything about its quality.
    structural = {"no_commit_in_repository", "no_parent_commit", "no_python_change"}
    opinions = set(funnel.dropped) - structural

    assert not opinions, f"mining rejected candidates on judgement, not fact: {sorted(opinions)}"


def test_the_pool_is_ranked_rather_than_pruned(adversarial: Path) -> None:
    """Everything that could become a task is present, in a defensible order.

    The sweep is not absent from the pool — it is last in it. That is the whole
    difference: a wrong prediction costs a position, and the gates still decide.
    """
    candidates, _, _ = mined(adversarial)
    order = [candidate.subject for candidate in candidates]

    assert set(order) == {"PR#1", "PR#2", "PR#4", "PR#5"}
    assert order.index("PR#1") < order.index("PR#5"), "the feature must outrank the sweep"
