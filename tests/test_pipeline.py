from __future__ import annotations

from types import SimpleNamespace

from stress_stack.pipeline import _semantic_failure


def test_returned_failure_statuses_are_not_reported_as_success() -> None:
    assert _semantic_failure("hygiene", SimpleNamespace(status="regressed"))
    assert _semantic_failure("container", SimpleNamespace(status="baseline_mismatch"))
    assert _semantic_failure("coverage", {"coverage": "unavailable"})
    assert _semantic_failure("bundle", {"missing": ["tasks.json"], "task_count": 0})


def test_complete_stage_results_have_no_semantic_failure() -> None:
    assert _semantic_failure("hygiene", SimpleNamespace(status="complete")) is None
    assert _semantic_failure("container", SimpleNamespace(status="verified")) is None
    assert _semantic_failure("coverage", {"coverage": "available"}) is None
    assert _semantic_failure("bundle", {"missing": [], "task_count": 10}) is None

