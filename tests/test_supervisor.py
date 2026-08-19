"""The pipeline loop repairs a failing stage instead of ending the run.

`run_pipeline` used to `break` on the first non-optional failure. Several of
those failures carry enough information to act on — a workflow capability whose
probe rejected its command can be asked again carrying the reason — so a stage
gets a bounded second chance and every attempt is recorded.

Two things must stay true, and they pull in opposite directions: a run that
succeeded on the retry must not read like one that succeeded outright, and a
stage with nothing to repair from must still fail fast rather than re-running
an identical failure.
"""

from __future__ import annotations

import stress_stack.pipeline as pipeline
from stress_stack.pipeline import PipelineResult, StageResult


def test_a_repaired_stage_records_the_attempt_that_failed() -> None:
    result = StageResult(
        "workflow_measured",
        "ok",
        3.0,
        "",
        attempts=[{"attempt": 1, "detail": "no usable workflow for: coverage"}],
    )

    payload = result.to_dict()

    assert payload["status"] == "ok"
    assert payload["attempts"][0]["detail"].startswith("no usable workflow")


def test_a_clean_stage_carries_no_attempts_key() -> None:
    """A run that worked first time must not grow noise describing that."""
    assert "attempts" not in StageResult("graph", "ok", 0.5).to_dict()


def test_only_stages_whose_failure_is_actionable_are_retried() -> None:
    """A repair with nothing to repair from is a second identical run."""
    assert "ingest" not in pipeline._REPAIRABLE
    assert "validate" not in pipeline._REPAIRABLE
    assert "emit" not in pipeline._REPAIRABLE
    # Every repairable stage reads a workflow capability, which is the one thing
    # `_repair` knows how to re-derive.
    assert pipeline._REPAIRABLE <= {
        "workflow", "workflow_measured", "hygiene", "deps", "coverage"
    }


def test_repair_is_bounded() -> None:
    """A stage still failing after seeing its own error twice is not converging."""
    assert pipeline._MAX_STAGE_ATTEMPTS == 2


def test_repair_declines_when_there_is_no_model_to_ask(tmp_path, monkeypatch) -> None:
    """Without one, the only available repair is re-probing the default that failed."""

    class Unconfigured:
        configured = False

        def __init__(self, *args, **kwargs) -> None:
            pass

    monkeypatch.setattr("stress_stack.openrouter.OpenRouterClient", Unconfigured)

    acted = pipeline._repair(
        "workflow", {"source": str(tmp_path), "profile": None}, "probe failed"
    )

    assert acted is False


def test_an_unrepairable_failure_still_stops_the_run(tmp_path) -> None:
    """Ingest cannot be repaired, so a bad source ends the run at one attempt."""
    result = pipeline.run_pipeline(str(tmp_path / "does-not-exist"), cwd=tmp_path)

    assert [stage.name for stage in result.stages] == ["ingest"]
    assert result.stages[0].status == "failed"
    assert result.stages[0].attempts == []
    assert not result.ok


def test_deliverable_complete_is_not_implied_by_ok() -> None:
    """A run that skipped task generation has no failures and no deliverable."""
    result = PipelineResult(
        stages=[StageResult("ingest", "ok", 0.1), StageResult("mine", "skipped", 0.0)]
    )

    assert result.ok
    assert not result.deliverable_complete
