"""The ``validate`` stage: turn ranked candidates into validated tasks.

Validation runs before selection, and deliberately over-produces. Selecting ten
tasks and then discovering that one fails its fail-before check forces a re-plan
of the whole corpus; validating a surplus and selecting from what survived
absorbs the same failure by taking the next candidate off the ranked pool.

Nothing here decides *which* tasks ship. It decides which ones *could*.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stress_stack.atomic import atomic_write_json
from stress_stack.candidates import EXCISION, HISTORY, Candidate
from stress_stack.errors import StressStackError
from stress_stack.excision import NEUTRAL, EXPLICIT
from stress_stack.git_repository import GitRepository
from stress_stack.graph import RepositoryGraph
from stress_stack.runner import Runner
from stress_stack.runtime_matrix import Era, RuntimeImages
from stress_stack.tasks import (
    STRICT,
    BuiltTask,
    TaskBuildError,
    build_evaluation_tree,
    scope_files,
    stage_excision_task,
    stage_history_task,
    validate_task,
)


@dataclass
class ValidationSummary:
    attempted: int = 0
    eligible: int = 0
    rejected: dict[str, list[str]] = field(default_factory=dict)
    seconds: float = 0.0
    # Candidates that were already in flight when the pool hit its target. A
    # serial run would never have reached them, so `attempted` is otherwise
    # unexplainable against `stop_after`. Their results are kept rather than
    # discarded: they are paid-for validated tasks, and selection consumes a
    # surplus.
    overrun: int = 0

    def reject(self, reason: str, task_id: str) -> None:
        self.rejected.setdefault(reason.split(":")[0], []).append(task_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "eligible": self.eligible,
            "overrun": self.overrun,
            "seconds": round(self.seconds, 1),
            "rejected_counts": {k: len(v) for k, v in sorted(self.rejected.items())},
            "rejected": {k: sorted(v) for k, v in sorted(self.rejected.items())},
        }


def task_identifier(candidate: Candidate) -> str:
    return candidate.candidate_id


def build_and_validate(
    repository: GitRepository,
    graph: RepositoryGraph,
    candidate: Candidate,
    tasks_root: Path,
    work_root: Path,
    runner: Runner,
    *,
    repeats: int,
    policy: str = STRICT,
    runtime: RuntimeImages | None = None,
    era: Era | None = None,
) -> BuiltTask:
    """Stage one candidate and put it through every gate.

    An excision candidate is tried with the neutral stub first. A covering test
    that fails on an ``assert`` against a hollow function is the strongest
    evidence available — the test pins behaviour, not the shape of an exception
    — so the stronger stub is preferred and the explicit one is the fallback
    rather than the default.

    ``runtime`` decides *where* that happens. When supplied, the runner is
    chosen from the candidate's own tree rather than from HEAD — see
    :mod:`stress_stack.runtime_matrix` for why a repository the test runner
    depends on cannot be judged in HEAD's image at all.
    """
    task_id = task_identifier(candidate)
    task_root = tasks_root / task_id
    task_root.mkdir(parents=True, exist_ok=True)
    built = BuiltTask(
        task_id=task_id, source=candidate.source, candidate=candidate, task_root=task_root
    )

    try:
        if candidate.source == HISTORY:
            transition, designated = stage_history_task(repository, candidate, task_root)
            built.detail["transition"] = transition.to_dict()
            built.detail["designated"] = designated
            changed = [str(p) for p in candidate.signals["source_paths"]]
        else:
            transition, designated, plan = stage_excision_task(
                repository, candidate, task_root, strategy=NEUTRAL
            )
            built.detail["transition"] = transition.to_dict()
            built.detail["designated"] = designated
            built.detail["excision"] = plan
            changed = [str(candidate.signals["path"])]
    except (TaskBuildError, StressStackError) as exc:
        built.rejected = f"staging_failed: {exc}"
        return built

    built.verifier_files = _files_in(task_root / "verifier")
    built.files_in_scope = scope_files(graph, changed)

    runner = _resolve_runner(built, runner, runtime, era)

    evaluation_tree = work_root / task_id
    build_evaluation_tree(task_root, evaluation_tree)
    try:
        validate_task(
            built, runner, evaluation_tree=evaluation_tree, repeats=repeats, policy=policy
        )
        if (
            candidate.source == EXCISION
            and not built.eligible
            and _should_retry_explicit(built)
        ):
            built = _retry_with_explicit_stub(
                repository, graph, candidate, tasks_root, work_root, runner,
                repeats=repeats, policy=policy,
            )
    finally:
        shutil.rmtree(evaluation_tree, ignore_errors=True)
    return built


def validate_pool(
    repository: GitRepository,
    graph: RepositoryGraph,
    candidates: list[Candidate],
    tasks_root: Path,
    work_root: Path,
    runner: Runner,
    *,
    limit: int,
    repeats: int,
    policy: str = STRICT,
    stop_after: int | None = None,
    minimum_modules: int = 0,
    existing_modules: set[str] | None = None,
    max_workers: int = 1,
    runtime: RuntimeImages | None = None,
    eras: dict[str, Era] | None = None,
) -> tuple[list[BuiltTask], ValidationSummary]:
    """Validate down the ranked pool until enough survive, or the budget runs out.

    Candidates are validated concurrently but *consumed in ranked order*: the
    consumer blocks on ``futures[cursor]`` rather than taking whichever finishes
    first, so the pool decides nothing about which candidates survive. Only this
    thread touches the summary, the module set or the reporter; a worker returns
    a ``BuiltTask`` and nothing else.

    A sliding window keeps exactly ``max_workers`` in flight, which bounds how
    far past the stop point the pool can run. Those overrun candidates are
    waited for rather than cancelled — a cancelled future's evaluation tree
    would never be cleaned up, and letting them finish keeps the result set
    identical from run to run, which a race between shutdown and completion
    would not.
    """
    from concurrent.futures import Future, ThreadPoolExecutor

    from stress_stack.progress import reporter

    live = reporter()
    summary = ValidationSummary()
    built: list[BuiltTask] = []
    started = time.monotonic()
    planned = candidates[:limit]
    workers = max(1, min(max_workers, len(planned) or 1))
    modules = set(existing_modules or ())

    def _validate(candidate: Candidate) -> BuiltTask:
        return build_and_validate(
            repository, graph, candidate, tasks_root, work_root, runner,
            repeats=repeats, policy=policy, runtime=runtime,
            era=(eras or {}).get(candidate.candidate_id),
        )

    def _record(result: BuiltTask) -> None:
        summary.attempted += 1
        built.append(result)
        if result.eligible:
            summary.eligible += 1
            modules.update(result.candidate.modules)
            if result.candidate.primary_module:
                modules.add(result.candidate.primary_module)
        else:
            summary.reject(result.rejected or _first_failed_gate(result), result.task_id)

    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="validate")
    futures: list[Future[BuiltTask]] = []
    submitted = 0
    cursor = 0
    try:
        while submitted < len(planned) and submitted < workers:
            futures.append(executor.submit(_validate, planned[submitted]))
            submitted += 1

        while cursor < len(futures):
            result = futures[cursor].result()
            cursor += 1
            _record(result)
            live.step(
                f"{result.task_id}  ({summary.eligible} eligible so far)",
                summary.attempted,
                len(planned),
            )
            if (
                stop_after is not None
                and summary.eligible >= stop_after
                and len(modules) >= minimum_modules
            ):
                break
            if submitted < len(planned):
                futures.append(executor.submit(_validate, planned[submitted]))
                submitted += 1

        # Whatever was already in flight when the target was reached. Ranked
        # order is preserved because these are the next entries in `futures`.
        for future in futures[cursor:]:
            summary.overrun += 1
            _record(future.result())
    finally:
        executor.shutdown(wait=True)

    summary.seconds = time.monotonic() - started
    return built, summary


def write_validation(path: Path, built: list[BuiltTask], summary: ValidationSummary) -> None:
    atomic_write_json(
        path,
        {
            "schema_version": "0.1.0",
            "summary": summary.to_dict(),
            "tasks": [task.to_dict() for task in built],
        },
    )


def _should_retry_explicit(built: BuiltTask) -> bool:
    """Only a fail-before that did not fail hard enough is worth a second stub.

    A neutral body that leaves the covering tests passing means those tests do
    not pin the behaviour at all — retrying with a louder stub would manufacture
    a failure rather than discover one, so that case is left rejected.
    """
    for verdict in built.gates:
        if verdict.gate != "fail_before":
            continue
        return verdict.reason_code == "failed_for_wrong_reason"
    return False


def _retry_with_explicit_stub(
    repository: GitRepository,
    graph: RepositoryGraph,
    candidate: Candidate,
    tasks_root: Path,
    work_root: Path,
    runner: Runner,
    *,
    repeats: int,
    policy: str = STRICT,
) -> BuiltTask:
    task_id = task_identifier(candidate)
    task_root = tasks_root / task_id
    built = BuiltTask(
        task_id=task_id, source=candidate.source, candidate=candidate, task_root=task_root
    )
    try:
        transition, designated, plan = stage_excision_task(
            repository, candidate, task_root, strategy=EXPLICIT
        )
    except (TaskBuildError, StressStackError) as exc:
        built.rejected = f"staging_failed: {exc}"
        return built

    built.detail["transition"] = transition.to_dict()
    built.detail["designated"] = designated
    built.detail["excision"] = plan
    built.detail["stub_retry"] = "neutral_stub_failed_for_wrong_reason"
    built.verifier_files = _files_in(task_root / "verifier")
    built.files_in_scope = scope_files(graph, [str(candidate.signals["path"])])

    evaluation_tree = work_root / task_id
    build_evaluation_tree(task_root, evaluation_tree)
    try:
        validate_task(
            built, runner, evaluation_tree=evaluation_tree, repeats=repeats, policy=policy
        )
    finally:
        shutil.rmtree(evaluation_tree, ignore_errors=True)
    return built


def _resolve_runner(
    built: BuiltTask,
    default: Runner,
    runtime: RuntimeImages | None,
    era: Era | None = None,
) -> Runner:
    """Pick the environment this candidate is judged in, and record the choice.

    Recorded on the task rather than only in the run log, because "which image
    was this verdict reached in" is part of the verdict when the answer is no
    longer the same for every task.
    """
    if runtime is None:
        return default
    from stress_stack.runner import select_runner

    image, provenance = runtime.runtime_for(built.task_root / "input", era=era)
    built.detail["runtime"] = provenance
    if image == getattr(default, "image", None):
        return default
    return select_runner(image=image)


def _first_failed_gate(built: BuiltTask) -> str:
    for verdict in built.gates:
        if not verdict.passed:
            return f"{verdict.gate}:{verdict.reason_code}"
    return "unknown"


def _files_in(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())
