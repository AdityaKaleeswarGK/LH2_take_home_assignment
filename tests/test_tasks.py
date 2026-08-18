"""How ``validate_task`` schedules its runs, and against which trees.

The gates themselves are covered by ``test_verification`` and ``test_screen``.
What is covered here is the part that changed when the verifier runs stopped
being sequential: Phase A must still be ordered, because each of its runs can
end the candidate, and the two verifier lanes must stay bound to their own tree.
Handing a lane the wrong tree would swap fail-before for pass-after and produce
a confidently wrong verdict.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from stress_stack import tasks as tasks_module
from stress_stack.candidates import EXCISION, Candidate
from stress_stack.runner import RunOutcome
from stress_stack.tasks import BuiltTask, build_evaluation_tree, validate_task
from stress_stack.verification import ASSERTION, PASSED, CaseResult, RunReport

TARGET = "tests/test_core.py::test_feature"
OTHER = "tests/test_core.py::test_stable"


def report(failing: bool) -> RunReport:
    built = RunReport()
    built.results[TARGET] = (
        CaseResult(TARGET, "failed", ASSERTION, "sig")
        if failing
        else CaseResult(TARGET, PASSED, PASSED, "sig")
    )
    built.results[OTHER] = CaseResult(OTHER, PASSED, PASSED, "sig")
    return built


class RecordingRunner:
    """Records every run, when it started and finished, and against what."""

    backend = "fake"

    def __init__(self, *, delay: float = 0.0) -> None:
        self.calls: list[dict] = []
        self._lock = threading.Lock()
        self._delay = delay

    def execute(
        self, tree: Path, evidence: Path, name: str, targets: list[str] | None = None
    ) -> RunOutcome:
        started = time.monotonic()
        with self._lock:
            order = len(self.calls)
        if self._delay:
            time.sleep(self._delay)
        entry = {
            "name": name,
            "tree": tree.name,
            "targets": list(targets or []),
            "order": order,
            "started": started,
            "finished": time.monotonic(),
        }
        with self._lock:
            self.calls.append(entry)
        # `before_*` runs see the pre-change tree, where the target fails.
        return RunOutcome(
            name=name,
            report=report(failing=name.startswith("before") or name == "baseline_full"),
            exit_code=1,
            seconds=0.01,
            backend=self.backend,
            infrastructure_failure=None,
        )

    def named(self, name: str) -> dict:
        return next(call for call in self.calls if call["name"] == name)


@pytest.fixture
def built(tmp_path: Path) -> BuiltTask:
    """A staged excision task: `input/` and `solution/` differ, `verifier/` is ready."""
    task_root = tmp_path / "task"
    for tree in ("input", "solution", "verifier"):
        (task_root / tree).mkdir(parents=True)

    for tree, body in (("input", "def feature():\n    ...\n"), ("solution", "def feature():\n    return 1\n")):
        package = task_root / tree / "pkg"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "core.py").write_text(body, encoding="utf-8")
        test_dir = task_root / tree / "tests"
        test_dir.mkdir()
        (test_dir / "test_core.py").write_text(
            "def test_feature():\n    assert True\n\n\ndef test_stable():\n    assert True\n",
            encoding="utf-8",
        )

    (task_root / "verifier" / "tests").mkdir()
    (task_root / "verifier" / "tests" / "test_core.py").write_text(
        (task_root / "solution" / "tests" / "test_core.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    candidate = Candidate(
        candidate_id="excise-pkg-core-feature",
        source=EXCISION,
        subject="pkg.core.feature",
        title="restore feature",
        modules=["pkg.core"],
        primary_module="pkg.core",
        signals={"path": "pkg/core.py", "covering_tests": [TARGET]},
    )
    task = BuiltTask(
        task_id=candidate.candidate_id,
        source=EXCISION,
        candidate=candidate,
        task_root=task_root,
    )
    task.verifier_files = ["tests/test_core.py"]
    return task


def run(built: BuiltTask, runner: RecordingRunner, tmp_path: Path) -> BuiltTask:
    evaluation_tree = tmp_path / "work"
    build_evaluation_tree(built.task_root, evaluation_tree)
    return validate_task(built, runner, evaluation_tree=evaluation_tree, repeats=2)


def test_phase_a_runs_strictly_in_order(built: BuiltTask, tmp_path: Path) -> None:
    """Each Phase A run can end the candidate, so none may start early."""
    runner = RecordingRunner()
    run(built, runner, tmp_path)

    phase_a = ["after_full", "before_full", "baseline_full"]
    assert [call["name"] for call in runner.calls[:3]] == phase_a
    latest_phase_a = max(runner.named(name)["finished"] for name in phase_a)
    for call in runner.calls[3:]:
        assert call["started"] >= latest_phase_a


def test_each_lane_stays_bound_to_its_own_tree(built: BuiltTask, tmp_path: Path) -> None:
    runner = RecordingRunner()
    run(built, runner, tmp_path)

    for index in (1, 2):
        assert runner.named(f"before_targets_{index}")["tree"] == "work"
        assert runner.named(f"after_targets_{index}")["tree"] == "solution"
        assert runner.named(f"before_targets_{index}")["targets"] == [TARGET]
        assert runner.named(f"after_targets_{index}")["targets"] == [TARGET]


def test_the_two_lanes_overlap(built: BuiltTask, tmp_path: Path) -> None:
    """The whole point of the change: before and after do not queue behind each other."""
    runner = RecordingRunner(delay=0.05)
    run(built, runner, tmp_path)

    before = runner.named("before_targets_1")
    after = runner.named("after_targets_1")
    assert before["started"] < after["finished"]
    assert after["started"] < before["finished"]


def test_repeats_within_a_lane_stay_sequential(built: BuiltTask, tmp_path: Path) -> None:
    """The determinism gate compares them, so they must not contend with each other."""
    runner = RecordingRunner(delay=0.05)
    run(built, runner, tmp_path)

    for prefix in ("before_targets", "after_targets"):
        first, second = runner.named(f"{prefix}_1"), runner.named(f"{prefix}_2")
        assert first["finished"] <= second["started"]


def test_the_evaluation_tree_is_not_rebuilt_when_the_verifier_is_complete(
    built: BuiltTask, tmp_path: Path, monkeypatch
) -> None:
    """The rebuild is a full copy of the repository; most candidates do not need it."""
    rebuilds = []
    original = tasks_module.build_evaluation_tree

    def counting(task_root: Path, destination: Path) -> Path:
        rebuilds.append(destination)
        return original(task_root, destination)

    evaluation_tree = tmp_path / "work"
    original(built.task_root, evaluation_tree)
    monkeypatch.setattr(tasks_module, "build_evaluation_tree", counting)
    validate_task(built, RecordingRunner(), evaluation_tree=evaluation_tree, repeats=2)

    assert rebuilds == []


def test_the_evaluation_tree_is_rebuilt_when_a_target_file_was_missing(
    built: BuiltTask, tmp_path: Path, monkeypatch
) -> None:
    """A target whose test file pre-dates the change is materialised, then re-laid."""
    (built.task_root / "verifier" / "tests" / "test_core.py").unlink()
    built.verifier_files = []

    rebuilds = []
    original = tasks_module.build_evaluation_tree

    def counting(task_root: Path, destination: Path) -> Path:
        rebuilds.append(destination)
        return original(task_root, destination)

    evaluation_tree = tmp_path / "work"
    original(built.task_root, evaluation_tree)
    monkeypatch.setattr(tasks_module, "build_evaluation_tree", counting)
    validate_task(built, RecordingRunner(), evaluation_tree=evaluation_tree, repeats=2)

    assert rebuilds == [evaluation_tree]
    assert built.verifier_files == ["tests/test_core.py"]
