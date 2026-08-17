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


def test_skipped_task_generation_is_not_a_complete_deliverable() -> None:
    """A run with no failures and no emitted tasks must not report success."""
    from stress_stack.pipeline import PipelineResult, StageResult

    result = PipelineResult(
        stages=[
            StageResult("ingest", "ok", 0.1),
            StageResult("hygiene", "ok", 0.1),
            StageResult("container", "ok", 1.0),
            StageResult("emit", "skipped", 0.0, "not implemented for go"),
            StageResult("bundle", "skipped", 0.0, "not implemented for go"),
        ]
    )
    assert result.ok is True
    assert result.deliverable_complete is False


def test_emitted_run_is_a_complete_deliverable() -> None:
    from stress_stack.pipeline import PipelineResult, StageResult

    result = PipelineResult(
        stages=[
            StageResult("emit", "ok", 1.0),
            StageResult("bundle", "ok", 1.0),
        ]
    )
    assert result.deliverable_complete is True


def test_python_only_stages_cover_task_generation() -> None:
    """The ecosystem-aware stages must not be skipped for other languages.

    `graph` is on the aware side: it dispatches to the tree-sitter builder.
    Coverage and task generation still need pytest and `ast`.
    """
    from stress_stack.pipeline import _PYTHON_ONLY

    aware = {
        "ingest", "hygiene", "deps", "graph", "coverage", "container",
        "mine", "validate", "select", "adjudicate", "emit", "bundle",
    }
    assert aware.isdisjoint(_PYTHON_ONLY)
    # Still Python-only: generated tests use pytest idioms, and enrichment and
    # the SQLite projection read the Python graph's richer edge set.
    assert {"testgen", "enrich", "index"} <= _PYTHON_ONLY


def test_unverified_hygiene_cannot_pass_as_verified() -> None:
    """An ecosystem that measured no regressions must not report `complete`."""
    unverified = SimpleNamespace(status="complete", regressions_verified=False)
    assert _semantic_failure("hygiene", unverified)

    honest = SimpleNamespace(status="complete_unverified", regressions_verified=False)
    assert _semantic_failure("hygiene", honest) is None

    # Python measures, so it is still held to the stricter bar.
    measured = SimpleNamespace(status="complete_unverified", regressions_verified=True)
    assert _semantic_failure("hygiene", measured)


def test_dependency_gate_reads_the_doctor_report() -> None:
    """The deps gate must understand DependencyLockReport, not silently fail it."""
    locked = SimpleNamespace(
        measured=True, status="locked", test_environment_available=True, reason=""
    )
    assert _semantic_failure("deps", locked) is None

    unsupported = SimpleNamespace(
        measured=False, status="unsupported", test_environment_available=None, reason="no_cargo"
    )
    detail = _semantic_failure("deps", unsupported)
    assert detail and "no_cargo" in detail

    no_env = SimpleNamespace(
        measured=True, status="locked", test_environment_available=False, reason=""
    )
    assert _semantic_failure("deps", no_env) == "test environment is unavailable"


def test_legacy_dependency_artifacts_gate_is_unchanged() -> None:
    """The pre-existing shape must still be gated exactly as before."""
    legacy_ok = SimpleNamespace(lock={"status": "locked"}, environment_available=True)
    assert _semantic_failure("deps", legacy_ok) is None

    legacy_unlocked = SimpleNamespace(lock={"status": "skipped"}, environment_available=True)
    assert _semantic_failure("deps", legacy_unlocked)

