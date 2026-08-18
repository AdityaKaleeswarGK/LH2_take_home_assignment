"""The validate pool's contract: concurrency must not change which tasks ship.

Every test here drives ``validate_pool`` with ``build_and_validate`` replaced by
a deterministic stand-in. That is the whole point — the pool's job is scheduling,
and scheduling is exactly what a real container run would hide behind noise.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, cast

import pytest

from stress_stack import validate as validate_module
from stress_stack.candidates import EXCISION, HISTORY, Candidate
from stress_stack.tasks import BuiltTask
from stress_stack.verification import GateVerdict


def make_candidate(index: int, *, module: str | None = None, source: str = HISTORY) -> Candidate:
    return Candidate(
        candidate_id=f"pr-{index:03d}",
        source=source,
        subject=f"subject {index}",
        title=f"title {index}",
        modules=[module or f"pkg.module_{index}"],
        primary_module=module or f"pkg.module_{index}",
    )


def make_task(candidate: Candidate, *, eligible: bool, reason: str = "collateral") -> BuiltTask:
    built = BuiltTask(
        task_id=candidate.candidate_id,
        source=candidate.source,
        candidate=candidate,
        task_root=Path("/nonexistent") / candidate.candidate_id,
    )
    if eligible:
        built.gates = [GateVerdict("fail_before", True, None, {})]
    else:
        built.gates = [GateVerdict(reason, False, "measured_no", {})]
    return built


def install_stub(monkeypatch: pytest.MonkeyPatch, decide, *, record: list[str] | None = None):
    """Replace the expensive part of the pool with a pure function of the candidate."""

    def fake(
        repository, graph, candidate, tasks_root, work_root, runner,
        *, repeats, policy, runtime=None, era=None,
    ):
        if record is not None:
            record.append(candidate.candidate_id)
        return decide(candidate)

    monkeypatch.setattr(validate_module, "build_and_validate", fake)


def run_pool(candidates: list[Candidate], **kwargs):
    # repository, graph and runner are only ever handed to `build_and_validate`,
    # which the stub replaces, so the pool never dereferences them.
    unused = cast(Any, None)
    return validate_module.validate_pool(
        unused,
        unused,
        candidates,
        Path("/nonexistent/tasks"),
        Path("/nonexistent/work"),
        unused,
        limit=kwargs.pop("limit", len(candidates)),
        repeats=kwargs.pop("repeats", 2),
        **kwargs,
    )


# --------------------------------------------------------------------------
# The acceptance criterion: the pool decides nothing
# --------------------------------------------------------------------------


@pytest.mark.parametrize("workers", [1, 2, 4, 8])
def test_worker_count_does_not_change_the_result(monkeypatch, workers):
    """Same candidates, same verdicts, any width — identical output."""
    candidates = [make_candidate(i) for i in range(12)]
    # A mix of outcomes so both the eligible and the rejected paths are exercised.
    install_stub(
        monkeypatch,
        lambda c: make_task(c, eligible=int(c.candidate_id.split("-")[1]) % 3 != 0),
    )

    built, summary = run_pool(candidates, max_workers=workers)

    assert [task.task_id for task in built] == [c.candidate_id for c in candidates]
    assert summary.attempted == 12
    assert summary.eligible == 8
    assert summary.overrun == 0
    assert summary.rejected == {"collateral": ["pr-000", "pr-003", "pr-006", "pr-009"]}


def test_completion_order_does_not_reorder_the_result(monkeypatch):
    """A candidate that finishes first must not overtake one ranked above it.

    The stub sleeps in reverse rank order, so left to its own devices the pool
    would consume the *last* candidate first.
    """
    candidates = [make_candidate(i) for i in range(6)]

    def decide(candidate: Candidate) -> BuiltTask:
        index = int(candidate.candidate_id.split("-")[1])
        time.sleep((6 - index) * 0.01)
        return make_task(candidate, eligible=True)

    install_stub(monkeypatch, decide)
    built, _ = run_pool(candidates, max_workers=6)

    assert [task.task_id for task in built] == [c.candidate_id for c in candidates]


# --------------------------------------------------------------------------
# The early exit
# --------------------------------------------------------------------------


def test_serial_pool_stops_exactly_at_the_target(monkeypatch):
    attempted: list[str] = []
    candidates = [make_candidate(i) for i in range(20)]
    install_stub(monkeypatch, lambda c: make_task(c, eligible=True), record=attempted)

    built, summary = run_pool(candidates, max_workers=1, stop_after=3)

    assert summary.eligible == 3
    assert summary.overrun == 0
    assert len(attempted) == 3
    assert [task.task_id for task in built] == ["pr-000", "pr-001", "pr-002"]


def test_overrun_is_bounded_by_the_window_and_kept(monkeypatch):
    """Candidates already in flight when the target is hit are finished, not dropped.

    Cancelling them would strand their evaluation trees, and their results are
    validated tasks the selection stage can use as spares.
    """
    workers = 4
    candidates = [make_candidate(i) for i in range(20)]
    install_stub(monkeypatch, lambda c: make_task(c, eligible=True))

    built, summary = run_pool(candidates, max_workers=workers, stop_after=3)

    assert summary.overrun <= workers - 1
    assert summary.attempted == 3 + summary.overrun
    assert len(built) == summary.attempted
    # Still ranked: the overrun entries are the next candidates in order.
    assert [task.task_id for task in built] == [
        c.candidate_id for c in candidates[: summary.attempted]
    ]


def test_overrun_count_is_stable_across_repeats(monkeypatch):
    """The pool waits for in-flight work rather than racing shutdown against it.

    If overrun depended on which futures happened to finish first, the recorded
    artifact would differ between two runs of the same repository.
    """
    seen = set()
    for _ in range(5):
        candidates = [make_candidate(i) for i in range(20)]
        install_stub(monkeypatch, lambda c: make_task(c, eligible=True))
        _, summary = run_pool(candidates, max_workers=4, stop_after=3)
        seen.add((summary.attempted, summary.overrun))
    assert len(seen) == 1


def test_module_floor_holds_the_pool_open(monkeypatch):
    """`stop_after` alone must not stop a run that cannot meet the diversity floor."""
    # The first six candidates all sit in one module; diversity only arrives later.
    candidates = [make_candidate(i, module="pkg.same") for i in range(6)]
    candidates += [make_candidate(i) for i in range(6, 12)]
    install_stub(monkeypatch, lambda c: make_task(c, eligible=True))

    _, summary = run_pool(
        candidates, max_workers=1, stop_after=2, minimum_modules=4
    )

    # Two eligible arrive immediately, but four distinct modules do not exist
    # until three of the later candidates have also been validated.
    assert summary.eligible == 9


def test_existing_modules_count_towards_the_floor(monkeypatch):
    """Modules banked by an earlier source are not re-earned by this one."""
    candidates = [make_candidate(i, module="pkg.same") for i in range(8)]
    install_stub(monkeypatch, lambda c: make_task(c, eligible=True))

    _, summary = run_pool(
        candidates,
        max_workers=1,
        stop_after=2,
        minimum_modules=4,
        existing_modules={"a", "b", "c"},
    )

    assert summary.eligible == 2


def test_limit_truncates_the_ranked_pool(monkeypatch):
    attempted: list[str] = []
    candidates = [make_candidate(i) for i in range(20)]
    install_stub(monkeypatch, lambda c: make_task(c, eligible=False), record=attempted)

    _, summary = run_pool(candidates, limit=5, max_workers=4)

    assert summary.attempted == 5
    assert sorted(attempted) == ["pr-000", "pr-001", "pr-002", "pr-003", "pr-004"]


# --------------------------------------------------------------------------
# Mechanics
# --------------------------------------------------------------------------


def test_concurrency_never_exceeds_the_worker_count(monkeypatch):
    lock = threading.Lock()
    live = [0]
    peak = [0]
    candidates = [make_candidate(i) for i in range(16)]

    def decide(candidate: Candidate) -> BuiltTask:
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.01)
        with lock:
            live[0] -= 1
        return make_task(candidate, eligible=False)

    install_stub(monkeypatch, decide)
    run_pool(candidates, max_workers=3)

    assert peak[0] <= 3
    assert peak[0] > 1, "the pool ran serially; the window never opened"


def test_rejection_reason_falls_back_to_the_first_failed_gate(monkeypatch):
    candidates = [make_candidate(0)]

    def decide(candidate: Candidate) -> BuiltTask:
        built = make_task(candidate, eligible=False)
        built.gates = [
            GateVerdict("fail_before", True, None, {}),
            GateVerdict("collateral", False, "broke_a_passing_test", {}),
        ]
        return built

    install_stub(monkeypatch, decide)
    _, summary = run_pool(candidates, max_workers=2)

    assert summary.rejected == {"collateral": ["pr-000"]}


def test_an_explicit_rejection_outranks_the_gates(monkeypatch):
    candidates = [make_candidate(0, source=EXCISION)]

    def decide(candidate: Candidate) -> BuiltTask:
        built = make_task(candidate, eligible=False)
        built.rejected = "staging_failed: could not excise"
        return built

    install_stub(monkeypatch, decide)
    _, summary = run_pool(candidates, max_workers=2)

    assert summary.rejected == {"staging_failed": ["pr-000"]}


def test_an_empty_pool_is_not_an_error(monkeypatch):
    install_stub(monkeypatch, lambda c: make_task(c, eligible=True))
    built, summary = run_pool([], max_workers=4)
    assert built == []
    assert summary.attempted == 0


def test_a_worker_exception_propagates(monkeypatch):
    """A crash is a stage failure, as it was when the loop was serial."""
    candidates = [make_candidate(i) for i in range(4)]

    def decide(candidate: Candidate) -> BuiltTask:
        if candidate.candidate_id == "pr-002":
            raise RuntimeError("boom")
        return make_task(candidate, eligible=True)

    install_stub(monkeypatch, decide)
    with pytest.raises(RuntimeError, match="boom"):
        run_pool(candidates, max_workers=2)
