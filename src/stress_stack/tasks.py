"""Materialise candidates into tasks and put them through the gates.

The shape of a task follows from one decision: **the verifier is applied at
evaluation time, not shipped in ``input/``.** A history-derived task's tests are
the ones the pull request added, and an agent that can read them does not have
to understand the instruction — it transcribes the assertions. So ``input/`` is
the repository as it stood before the change, tests included as they stood then,
and the post-change tests live in ``verifier/`` where the harness applies them.

That makes four trees per task, three of which are written down:

* ``input/`` — what a solver receives. Pre-change source, pre-change tests.
* ``solution/`` — the reference. Post-change everything.
* ``verifier/`` — the test files evaluation overlays onto a solver's answer,
  plus the exact node ids that decide the verdict.
* the evaluation tree — ``input/`` with ``verifier/`` laid over it, rebuilt on
  demand. Anyone can reconstruct it from the three above, which is what makes
  the fail-before claim checkable rather than merely reported.

An excision task uses the same shape with the same code: its verifier files are
the covering tests, unchanged, so the overlay is a no-op and every downstream
stage stays written once.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stress_stack.candidates import EXCISION, HISTORY, Candidate
from stress_stack.errors import GitError, StressStackError
from stress_stack.excision import EXPLICIT, ExcisionError, excise, plan_excision
from stress_stack.git_repository import GitRepository
from stress_stack.graph import RepositoryGraph, blast_radius
from stress_stack.runner import RunOutcome, Runner, forget_source_roots, pytest_argument
from stress_stack.runtime_matrix import RUNNER_DEPENDENCIES
from stress_stack.snapshot import (
    OVERLAY_FILES,
    synthesize_version_modules,
    Transition,
    apply_overlay,
    audit_solver_bundle,
    golden_diff,
    materialize_tree,
    purge_bytecode,
    resolve_transition,
)
from stress_stack.verification import (
    ASSERTION,
    BEHAVIORAL_EXCEPTION,
    INFRASTRUCTURE,
    PASSED,
    GateVerdict,

    gate_collateral,
    gate_determinism,
    gate_fail_before,
    gate_pass_after,
    resolve_targets,
    gate_verifier_integrity,
    summarize,
)

DEFAULT_REPEATS = 2


class TaskBuildError(StressStackError):
    """This candidate cannot be turned into a task; it is rejected, not fatal."""


@dataclass
class BuiltTask:
    task_id: str
    source: str
    candidate: Candidate
    task_root: Path
    targets: list[str] = field(default_factory=list)
    verifier_files: list[str] = field(default_factory=list)
    files_in_scope: list[str] = field(default_factory=list)
    outcomes: list[RunOutcome] = field(default_factory=list)
    gates: list[GateVerdict] = field(default_factory=list)
    rejected: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def eligible(self) -> bool:
        return self.rejected is None and bool(self.gates) and all(g.passed for g in self.gates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "source": self.source,
            "candidate_id": self.candidate.candidate_id,
            "subject": self.candidate.subject,
            "title": self.candidate.title,
            "primary_module": self.candidate.primary_module,
            "modules": self.candidate.modules,
            # Carried through so selection and difficulty read one artifact
            # rather than re-joining validation back onto candidates.
            "score": round(self.candidate.score, 4),
            "signals": self.candidate.signals,
            "rejected": self.rejected,
            "targets": self.targets,
            "verifier_files": self.verifier_files,
            "files_in_scope": self.files_in_scope,
            "detail": self.detail,
            "runs": [outcome.to_dict() for outcome in self.outcomes],
            # Stated structurally rather than by later-wins ordering: the gate
            # roll-up must not be able to overwrite the authoritative answer.
            **{**summarize(self.gates), "eligible": self.eligible},
        }


# --------------------------------------------------------------------------
# Discovering what a change's tests actually are
# --------------------------------------------------------------------------


def collected_tests(source: str) -> dict[str, str]:
    """pytest-collectable test functions, keyed by their dotted name in the module."""
    return collected_tests_with_status(source)[0]


def collected_tests_with_status(source: str) -> tuple[dict[str, str], str | None]:
    """The tests, plus why there were none when the reason was a parse failure.

    Mirrors pytest's defaults — ``test_*`` functions, and ``test_*`` methods on
    ``Test*`` classes that define no ``__init__``. The value is the unparsed
    body, so "this test changed" is a comparison rather than a guess.

    The status exists because history is parsed with the *host* interpreter
    while the trees themselves are historical, so a file using syntax this
    interpreter cannot read is not unusual. An empty dict alone cannot be told
    apart from "this file has no tests", and the difference matters: the count
    of added test functions is the largest single term in candidate ranking, so
    a parse failure silently sends a good pull request to the bottom of the
    pool. HEAD already records its syntax errors — see ``symbols.ParsedFile`` —
    and this is the same accounting for history.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return {}, f"{type(exc).__name__}: {exc}"

    found: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test_"
        ):
            found[node.name] = ast.unparse(node)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            if any(
                isinstance(child, ast.FunctionDef) and child.name == "__init__"
                for child in node.body
            ):
                continue
            for child in node.body:
                if isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef)
                ) and child.name.startswith("test_"):
                    found[f"{node.name}.{child.name}"] = ast.unparse(child)
    return found, None


def show(repository: GitRepository, sha: str, path: str) -> str:
    """A file's contents at a revision, or empty when it did not exist there."""
    try:
        return repository.run(["show", f"{sha}:{path}"], record=False)
    except GitError:
        return ""


def changed_test_functions(
    repository: GitRepository, transition: Transition, test_paths: list[str]
) -> dict[str, list[str]]:
    """Which test functions the change added or rewrote, per file.

    Only these decide the verdict. Running every test the file happens to
    contain would let a task pass on assertions that were already passing before
    the change, which is not evidence of anything.
    """
    designated: dict[str, list[str]] = {}
    for path in test_paths:
        before = collected_tests(show(repository, transition.base_sha, path))
        after = collected_tests(show(repository, transition.head_sha, path))
        names = sorted(
            name
            for name, body in after.items()
            if name not in before or before[name] != body
        )
        if names:
            designated[path] = names
    return designated




# --------------------------------------------------------------------------
# Materialisation
# --------------------------------------------------------------------------


def stage_history_task(
    repository: GitRepository, candidate: Candidate, task_root: Path
) -> tuple[Transition, dict[str, list[str]]]:
    """input/ = the tree before the change; solution/ = the tree after it."""
    transition = resolve_transition(
        repository,
        str(candidate.signals["head_sha"]),
        linkage_method="github_merge_sha",
    )
    input_dir, solution_dir = _fresh(task_root)
    materialize_tree(repository, transition.base_sha, input_dir)
    materialize_tree(repository, transition.head_sha, solution_dir)
    apply_overlay(repository.root, input_dir)
    apply_overlay(repository.root, solution_dir)
    synthesize_version_modules(repository, transition.base_sha, input_dir)
    synthesize_version_modules(repository, transition.head_sha, solution_dir)

    # A change that touched no test file gets an empty verifier, and that is a
    # complete task rather than a broken one: the tests that prove it are
    # already in `input/`, committed alongside the bug they pin. Refusing here
    # was the last static filter in the path, and it discarded exactly the
    # candidates whose fail-before evidence needs no construction at all.
    designated = changed_test_functions(
        repository, transition, [str(p) for p in candidate.signals["test_paths"]]
    )

    verifier_root = task_root / "verifier"
    _reset(verifier_root)
    for path in designated:
        content = show(repository, transition.head_sha, path)
        target = verifier_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    # A test is rarely self-contained in one .py file. Copy every changed file
    # under the test roots (fixtures, conftest modules, data files, snapshots)
    # so fail-before cannot be manufactured by a missing asset such as Glom's
    # test_valid.toml.
    for path in _verifier_assets(candidate):
        source = solution_dir / path
        if not source.is_file():
            continue
        target = verifier_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    (task_root / "goldenSolution.diff").write_text(
        golden_diff(repository, transition), encoding="utf-8"
    )
    return transition, designated


def _verifier_assets(candidate: Candidate) -> list[str]:
    test_paths = [str(path) for path in candidate.signals.get("test_paths") or []]
    changed_paths = [str(path) for path in candidate.signals.get("changed_paths") or []]
    if not test_paths:
        return []
    common = Path(os.path.commonpath(test_paths)).as_posix()
    if common.endswith(".py"):
        common = Path(common).parent.as_posix()
    prefix = common.rstrip("/") + "/"
    return sorted(
        path
        for path in changed_paths
        if path.startswith(prefix) or Path(path).name == "conftest.py"
    )


def stage_excision_task(
    repository: GitRepository,
    candidate: Candidate,
    task_root: Path,
    *,
    strategy: str = EXPLICIT,
) -> tuple[Transition, dict[str, list[str]], dict[str, Any]]:
    """input/ = HEAD with one body removed; solution/ = HEAD untouched."""
    head_sha = repository.run(["rev-parse", "HEAD"], record=False).strip()
    transition = Transition(
        head_sha=head_sha,
        base_sha=head_sha,
        parent_index=0,
        head_tree=repository.run(["rev-parse", "HEAD^{tree}"], record=False).strip(),
        base_tree=repository.run(["rev-parse", "HEAD^{tree}"], record=False).strip(),
        is_merge=False,
        linkage_method="excision",
    )
    input_dir, solution_dir = _fresh(task_root)
    materialize_tree(repository, head_sha, input_dir)
    materialize_tree(repository, head_sha, solution_dir)
    apply_overlay(repository.root, input_dir)
    apply_overlay(repository.root, solution_dir)
    synthesize_version_modules(repository, transition.base_sha, input_dir)
    synthesize_version_modules(repository, transition.head_sha, solution_dir)

    path = str(candidate.signals["path"])
    qualified = str(candidate.signals["qualified_name"])
    module = _module_prefix(qualified, path)
    source_file = input_dir / path
    if not source_file.is_file():
        raise TaskBuildError(f"{path} is not present in the materialised tree")

    original = source_file.read_text(encoding="utf-8")
    dotted = qualified[len(module) + 1 :] if module else qualified
    name = qualified.rsplit(".", 1)[-1]
    if path.endswith(".py"):
        try:
            # Ask the plan which stubs are meaningful before honouring a
            # preference: a generator has no neutral body, and requesting one
            # anyway would build a task whose failure says nothing about the
            # contract.
            plan = plan_excision(original, name, path=path, dotted=dotted)
            chosen = strategy if strategy in plan.strategies() else plan.strategies()[0]
            result = excise(original, name, strategy=chosen, path=path, dotted=dotted)
        except ExcisionError as exc:
            raise TaskBuildError(str(exc)) from exc
    else:
        # Other ecosystems excise through tree-sitter, which knows the body's
        # exact extent. There is no strategy choice: each language has one
        # idiomatic unimplemented marker (`todo!()`, `panic`, `throw`).
        from stress_stack.excision_multilang import excise_symbol
        from stress_stack.workflow import STUB, load_workflow

        # The marker comes from the resolved workflow when there is one. It only
        # gets there by being excised into a real symbol in this repository and
        # re-parsed, so it is a measured answer rather than a table entry — and
        # for an ecosystem the table has no row for, it is the only answer.
        workflow = load_workflow(repository.root / ".stress_stack" / "workflow.json")
        stub = workflow.get(STUB) if workflow else None
        result = excise_symbol(
            path, original, name, marker=stub.settings.get("marker") if stub else None
        )
        if result is None:
            raise TaskBuildError(f"could not excise {name} from {path}")

    source_file.write_text(result.stubbed, encoding="utf-8")
    # The Python excision labels its diff on demand; the multi-language one
    # already carries the path in its headers.
    golden = result.diff(label=path) if path.endswith(".py") else result.diff()
    (task_root / "goldenSolution.diff").write_text(golden, encoding="utf-8")

    covering = [str(t) for t in candidate.signals["covering_tests"]]
    designated = _designate_tests(covering, solution_dir, path)
    verifier_root = task_root / "verifier"
    _reset(verifier_root)
    for test_path in designated:
        # Which tree the verifier copy comes from decides whether the excision
        # survives. Normally `solution/`: the tests are in their own file, and
        # the reference copy of that file is what should decide the verdict.
        #
        # Not when the tests live in the file the excision just cut. Rust puts
        # them there by convention — `#[cfg(test)] mod tests` sits at the bottom
        # of the module it exercises — and `build_evaluation_tree` lays the
        # verifier over `input/`, so taking that file from `solution/` puts the
        # implementation straight back on top of the stub. Every Rust candidate
        # then ran its fail-before against complete code and was rejected as
        # `no_test_changed_verdict`: the excision was real, and undone one step
        # later by the thing meant to test it.
        #
        # `input/` is the right source there. It holds the same file with the
        # tests intact and the body removed, so the verifier carries exactly the
        # tests that decide the verdict, contains no answer to read, and the
        # overlay becomes a no-op instead of a restoration.
        source_tree = input_dir if test_path == path else solution_dir
        target = verifier_root / test_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_tree / test_path, target)

    return transition, designated, result.to_dict()


def build_evaluation_tree(task_root: Path, destination: Path) -> Path:
    """``input/`` with ``verifier/`` laid over it — the tree fail-before runs in.

    Rebuilt rather than stored, because everything it is made of is already
    written down. A grader who copies ``input/``, copies ``verifier/`` over it
    and runs the recorded node ids reproduces the recorded verdict exactly.
    """
    _reset(destination)
    shutil.copytree(task_root / "input", destination, dirs_exist_ok=True)
    verifier_root = task_root / "verifier"
    if verifier_root.is_dir():
        for source in sorted(verifier_root.rglob("*")):
            if not source.is_file():
                continue
            target = destination / source.relative_to(verifier_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    purge_bytecode(destination)
    # This is the only place a tree is rewritten at a path that may already have
    # been run against, so it is the only place the PYTHONPATH cache can go
    # stale.
    forget_source_roots()
    return destination


# --------------------------------------------------------------------------
# Phase A: the screen
# --------------------------------------------------------------------------

STRICT = "strict"
MEASURED = "measured"


@dataclass
class Screen:
    """Which tests changed verdict across the transition, and in what way.

    This is the ground truth a task rests on, and it replaces every static
    guess about which tests "belong" to a change. Two classes are counted
    separately because they are not equally strong evidence:

    ``ran_and_failed``
        The test executed against the pre-change code and failed on an
        assertion or an exception raised from inside the repository, then
        passed against the reference. Nothing is inferred anywhere.

    ``absent_before``
        The test could not be collected before the change, because its file
        imports a name the change introduces. The feature really is missing —
        but the evidence is file-level rather than test-level, since pytest
        never reached the test body. Whether this counts is a reading of the
        brief's "a failure caused by an import error ... does not count", so it
        is recorded either way and a policy decides.
    """

    ran_and_failed: list[str] = field(default_factory=list)
    absent_before: list[str] = field(default_factory=list)
    failed_on_load: list[str] = field(default_factory=list)
    passing_both: int = 0
    collection_errors: list[str] = field(default_factory=list)

    def designated(self, policy: str) -> list[str]:
        if policy == MEASURED:
            return sorted(set(self.ran_and_failed) | set(self.absent_before))
        return sorted(self.ran_and_failed)

    def rejection(self, policy: str) -> str:
        if policy == STRICT and self.absent_before:
            return "only_uncollectable_tests_changed_verdict"
        return "no_test_changed_verdict"

    def to_dict(self) -> dict[str, Any]:
        return {
            "screen": {
                "ran_and_failed": sorted(self.ran_and_failed),
                "absent_before": sorted(self.absent_before),
                "failed_on_load": sorted(self.failed_on_load),
                "passing_both": self.passing_both,
                "collection_errors": sorted(self.collection_errors)[:10],
            }
        }


def screen_transition(
    before: RunOutcome, after: RunOutcome, *, policy: str = STRICT
) -> Screen:
    """Compare the two full runs and classify every verdict change.

    Designation is by measurement rather than by which files the diff touched.
    That matters in both directions: glom's Python 3.12 sweep rewrote two dozen
    test bodies without changing a single outcome, and a bug fix whose failing
    test was already committed changes an outcome without touching a test file
    at all. Only the run knows.
    """
    del policy  # classification is policy-free; only designation is not
    screen = Screen()
    before_results = before.report.results
    passing_after = after.report.passing()

    for test_id in sorted(passing_after):
        result = before_results.get(test_id)
        if result is None:
            screen.absent_before.append(test_id)
        elif result.status == PASSED:
            screen.passing_both += 1
        elif result.failure_class in {ASSERTION, BEHAVIORAL_EXCEPTION}:
            screen.ran_and_failed.append(test_id)
        else:
            screen.failed_on_load.append(test_id)

    screen.collection_errors = sorted(
        test_id
        for test_id, result in before_results.items()
        if result.failure_class == INFRASTRUCTURE
    )
    return screen


# --------------------------------------------------------------------------
# Phase B: the gates
# --------------------------------------------------------------------------


def validate_task(
    built: BuiltTask,
    runner: Runner,
    *,
    evaluation_tree: Path,
    repeats: int = DEFAULT_REPEATS,
    policy: str = STRICT,
) -> BuiltTask:
    """Run every gate and record both the verdicts and the runs behind them."""
    evidence = built.task_root / "evidence"
    solution_dir = built.task_root / "solution"

    after_full = runner.execute(solution_dir, evidence, "after_full")
    built.outcomes.append(after_full)
    if not after_full.usable:
        built.rejected = _reference_failure(built, after_full)
        return built

    before_full = runner.execute(evaluation_tree, evidence, "before_full")
    built.outcomes.append(before_full)
    if not before_full.usable:
        built.rejected = f"fail_before_run_failed: {before_full.infrastructure_failure}"
        built.detail["fail_before_detail"] = before_full.detail
        return built

    # --- Phase A ends here. Two runs have answered the only question that
    # decides whether this candidate can be a task at all, and a rejection
    # below costs nothing further.
    screen = screen_transition(before_full, after_full, policy=policy)
    built.detail.update(screen.to_dict())
    designated = screen.designated(policy)
    if not designated:
        built.rejected = screen.rejection(policy)
        return built

    # Designation comes from two *unfiltered* runs. Everything below re-runs the
    # suite narrowed to those ids, and not every id a runner reports is one it
    # can take back as a filter — `go test -run` cannot select an auto-generated
    # `#06` subtest segment, so the narrowed run collected nothing and twelve of
    # thirteen Go candidates were rejected as `targets_not_collected`. Resolve
    # against what the two full runs actually collected, before spending them.
    resolved, resolution = resolve_targets(
        designated,
        [before_full.report, after_full.report],
        selection_id=getattr(runner, "selection_id", None),
    )
    built.detail["designated_resolved"] = resolution
    built.targets = resolved
    if not resolved:
        built.rejected = screen.rejection(policy)
        return built


    # Candidates whose proving test pre-dates the fix still need a non-empty,
    # explicit verifier folder in the deliverable. Materialise those target
    # files now, and rebuild the evaluation tree only if that actually added
    # any — the rebuild is a full copy of the repository, and for most
    # candidates the verifier already carries every target file.
    verifier_before = list(built.verifier_files)
    _ensure_target_verifier_files(built, resolved)
    if built.verifier_files != verifier_before:
        build_evaluation_tree(built.task_root, evaluation_tree)

    # Collateral is a question about the *unmodified* pre-change tree: did the
    # reference solution break something that was passing before? Measuring it
    # on the evaluation tree was wrong — that tree carries the post-change test
    # files, so a verifier that cannot import yet destroys the baseline along
    # with the fail-before evidence, and two unrelated questions fail together.
    baseline_full = runner.execute(built.task_root / "input", evidence, "baseline_full")
    built.outcomes.append(baseline_full)
    if not baseline_full.usable:
        built.rejected = f"baseline_run_failed: {baseline_full.infrastructure_failure}"
        return built

    # The two sides are independent, so they run as concurrent lanes. The
    # repeats *within* a side stay sequential and ordered on purpose: the
    # determinism gate compares them against each other, and taking them under
    # different machine load would let contention masquerade as
    # nondeterminism. Two lanes keeps both repeats of a side under the same
    # conditions — one other lane running — while halving the wall clock.
    repeat_count = max(2, repeats)

    def _lane(tree: Path, prefix: str) -> list[RunOutcome]:
        return [
            runner.execute(tree, evidence, f"{prefix}_{index}", resolved)
            for index in range(1, repeat_count + 1)
        ]

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="gate") as pool:
        before_lane = pool.submit(_lane, evaluation_tree, "before_targets")
        after_lane = pool.submit(_lane, solution_dir, "after_targets")
        before_targets = before_lane.result()
        after_targets = after_lane.result()
    built.outcomes.extend(before_targets + after_targets)

    broken = [o for o in before_targets + after_targets if not o.usable]
    if broken:
        built.rejected = f"verifier_run_failed: {broken[0].infrastructure_failure}"
        return built

    targets = set(resolved)
    built.gates = [
        # Not require_assertion: classify() already documents that an absent
        # feature usually fails by raising from inside the library rather than
        # by tripping an assert, and that such a failure must qualify. The
        # brief disqualifies a failure to *load* the code, not an exception
        # raised by it. Demanding an assertion here rejected twenty otherwise
        # valid tasks on glom.
        gate_fail_before(before_targets[0].report, targets),
        gate_pass_after(after_targets[0].report, targets),
        gate_collateral(
            baseline_full.report,
            after_full.report,
            ignore=_deliberately_removed(baseline_full, after_full, built),
        ),
        _named(
            gate_determinism([o.report for o in before_targets], targets),
            "determinism_before",
        ),
        _named(
            gate_determinism([o.report for o in after_targets], targets),
            "determinism_after",
        ),
        gate_verifier_integrity(_verifier_sources(built.task_root)),
        _gate_bundle_clean(built.task_root / "input"),
        _gate_alternative(built, runner, evidence),
    ]
    built.detail["repeats"] = len(before_targets)
    built.detail["failure_classes"] = {
        target: before_targets[0].report.results[target].failure_class
        for target in sorted(targets)
        if target in before_targets[0].report.results
    }
    return built


def _reference_failure(built: BuiltTask, outcome: RunOutcome) -> str:
    """Name the cause when the reference tree cannot even be collected."""
    # `usable` is False for an empty run while `infrastructure_failure` stays
    # None — the descriptive string only exists in to_dict(). Testing the
    # attribute meant this never fired and the funnel said "None".
    if outcome.empty:
        tree = built.task_root / "solution"
        from stress_stack.runner import source_roots

        for root in source_roots(tree).split(":"):
            relative = root.removeprefix("/work").strip("/")
            directory = tree / relative if relative else tree
            if not directory.is_dir():
                continue
            for entry in directory.iterdir():
                name = entry.name.removesuffix(".py")
                if name in RUNNER_DEPENDENCIES and (
                    entry.is_dir() or entry.suffix == ".py"
                ):
                    return (
                        "runner_dependency_shadowed: the task tree provides "
                        f"{name!r}, which the test runner imports, so mounting it "
                        "replaces the copy pytest is running on"
                    )
    return f"reference_run_failed: {outcome.infrastructure_failure or 'collected_no_tests'}"


def _gate_alternative(built: BuiltTask, runner: Runner, evidence: Path) -> GateVerdict:
    """Would a *different* correct implementation also pass this verifier?

    Every other gate proves the reference satisfies the verifier, which the
    brief says is not enough: "a different but correct implementation must pass
    your verifier." Renaming the private symbols the change introduced cannot
    alter observable behaviour, so a verifier that then fails was pinned to
    structure rather than to behaviour.

    The static coupling scan is reported rather than gated on — it flags how
    portable a verifier looks, while the mutation measures whether it actually
    is.
    """
    from stress_stack.alternatives import rename_private, scan_coupling, verdict

    solution = built.task_root / "solution"
    changed = _changed_sources(built)
    private = set()
    for relative in changed:
        after_path = solution / relative
        before_path = built.task_root / "input" / relative
        if after_path.is_file():
            from stress_stack.alternatives import private_symbols

            after = private_symbols(
                after_path.read_text(encoding="utf-8", errors="replace")
            )
            before = (
                private_symbols(before_path.read_text(encoding="utf-8", errors="replace"))
                if before_path.is_file()
                else set()
            )
            private |= after - before
    coupling = scan_coupling(_verifier_sources(built.task_root), private)

    mutant = built.task_root / "alternative"
    if mutant.exists():
        shutil.rmtree(mutant)
    shutil.copytree(solution, mutant)
    mutation = rename_private(mutant, changed, names=private)

    ran, still_passing = False, False
    if mutation.renames:
        outcome = runner.execute(mutant, evidence, "alternative", built.targets)
        built.outcomes.append(outcome)
        ran = outcome.usable
        still_passing = ran and set(built.targets) <= outcome.report.passing()
    shutil.rmtree(mutant, ignore_errors=True)

    detail = verdict(mutation, still_passing, coupling, ran=ran or not mutation.renames)
    return GateVerdict(
        "alternative_implementation", bool(detail["portable"]),
        None if detail["portable"] else detail["status"], detail,
    )


def _changed_sources(built: BuiltTask) -> list[str]:
    signals = built.candidate.signals
    if built.source == EXCISION:
        return [str(signals.get("path") or "")]
    return [str(path) for path in (signals.get("source_paths") or [])]


def _ensure_target_verifier_files(built: BuiltTask, targets: list[str]) -> None:
    solution = built.task_root / "solution"
    verifier = built.task_root / "verifier"
    verifier.mkdir(parents=True, exist_ok=True)
    # Never the file the excision cut. Copying it from `solution/` would put the
    # implementation back over the stub — see `stage_excision_task`, which
    # refuses it for the same reason. This is the second door into the same
    # mistake, and a Rust module holding both the code and its `#[cfg(test)]`
    # tests walks into whichever one is left open.
    excised = str((built.detail.get("excision") or {}).get("path") or "")
    for test_id in targets:
        relative = pytest_argument(solution, test_id).split("::", 1)[0]
        if relative == excised:
            continue
        source = solution / relative
        destination = verifier / relative
        if source.is_file() and not destination.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    built.verifier_files = sorted(
        str(path.relative_to(verifier)) for path in verifier.rglob("*") if path.is_file()
    )


def _deliberately_removed(
    baseline: RunOutcome, after: RunOutcome, built: BuiltTask
) -> set[str]:
    """Tests present before and absent after, in files the change itself rewrote.

    Scoped to the verifier files on purpose. A test that vanishes from a file the
    change never touched is a collection regression and must still fail the gate;
    one that vanishes from a file the change rewrote was retired by the author.
    """
    vanished = set(baseline.report.results) - set(after.report.results)
    if not vanished:
        return set()
    solution = built.task_root / "solution"
    removed: set[str] = set()
    for test_id in vanished:
        argument = pytest_argument(built.task_root / "input", test_id)
        path, _, remainder = argument.partition("::")
        if path not in set(built.verifier_files) or not remainder:
            continue
        existing = collected_tests(
            (solution / path).read_text(encoding="utf-8", errors="replace")
        ) if (solution / path).is_file() else {}
        local = remainder.split("[", 1)[0].replace("::", ".")
        if local not in existing:
            removed.add(test_id)
    return removed



def scope_files(
    graph: RepositoryGraph, changed: list[str], *, limit: int = 25
) -> list[str]:
    """The files a solver plausibly has to read: what changed, plus what calls it.

    ``blast_radius`` keys its answer by the *changed* file, because the question
    it asks is "who references this". Iterating those keys therefore only ever
    yielded the changed set back, and every task shipped with a scope listing
    nothing a solver did not already know. The callers are in the entries.
    """
    changed_set = set(changed)
    radius = blast_radius(graph, changed_set)
    callers: list[str] = []
    for entries in radius["impacted"].values():
        for entry in entries:
            path = entry.get("caller_path")
            if path and path not in changed_set and path not in callers:
                callers.append(path)
    return (sorted(changed_set) + sorted(callers))[:limit]


def _named(verdict: GateVerdict, name: str) -> GateVerdict:
    return GateVerdict(name, verdict.passed, verdict.reason_code, verdict.detail)


def _gate_bundle_clean(input_dir: Path) -> GateVerdict:
    """A solver's copy must not contain the answer in any form."""
    audit = audit_solver_bundle(input_dir)
    return GateVerdict(
        "solver_bundle",
        bool(audit["clean"]),
        None if audit["clean"] else "answer_reachable_from_input",
        audit,
    )


def _verifier_sources(task_root: Path) -> dict[str, str]:
    verifier_root = task_root / "verifier"
    if not verifier_root.is_dir():
        return {}
    return {
        str(path.relative_to(verifier_root)): path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(verifier_root.rglob("*"))
        if path.is_file() and path.stat().st_size <= 1_000_000
    }


def _group_by_file(node_ids: list[str], tree: Path) -> dict[str, list[str]]:
    """Turn observed node ids back into {file: [local test names]}."""
    grouped: dict[str, list[str]] = {}
    for node_id in node_ids:
        argument = pytest_argument(tree, node_id)
        path, _, remainder = argument.partition("::")
        if not remainder or not (tree / path).is_file():
            continue
        grouped.setdefault(path, []).append(remainder.replace("::", "."))
    return {path: sorted(set(names)) for path, names in sorted(grouped.items())}


def _designate_tests(node_ids: list[str], tree: Path, subject_path: str) -> dict[str, list[str]]:
    """Map covering test ids to the files that define them.

    pytest's ids already carry the file (`glom/test/test_x.py::test_y`), so the
    Python path just splits them. Go's carry the *package*
    (`example.com/m::TestAdd`), which names a directory, not a file — so the
    defining file is found by looking for the declaration. Without this the
    verifier directory came out empty and every excision task failed staging.
    """
    if subject_path.endswith(".py"):
        return _group_by_file(node_ids, tree)

    grouped: dict[str, list[str]] = {}
    for node_id in node_ids:
        name = node_id.rsplit("::", 1)[-1]
        for candidate in sorted(tree.rglob("*")):
            if not candidate.is_file() or candidate.suffix != Path(subject_path).suffix:
                continue
            # Relative to the staged tree, never absolute: the tree itself lives
            # under `.stress_stack/tasks/<id>/solution`, so testing the absolute
            # parts excluded every file in it and designated nothing.
            if ".stress_stack" in candidate.relative_to(tree).parts:
                continue
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if re.search(rf"\b(fn|func)\s+{re.escape(name)}\s*[(<]", text):
                grouped.setdefault(candidate.relative_to(tree).as_posix(), []).append(name)
                break
    return {path: sorted(set(names)) for path, names in sorted(grouped.items())}


def _module_prefix(qualified: str, path: str) -> str:
    """The dotted module a qualified name starts with, inferred from its path.

    ``glom/core.py`` and ``glom.core.Path.from_t`` agree on the prefix; taking
    it from the path rather than by counting dots keeps a nested package right.
    """
    stem = path.removesuffix(".py").removesuffix("/__init__").replace("/", ".")
    parts = stem.split(".")
    for cut in range(len(parts), 0, -1):
        candidate = ".".join(parts[-cut:])
        if qualified.startswith(candidate + "."):
            return candidate
    return ""


def _fresh(task_root: Path) -> tuple[Path, Path]:
    input_dir = task_root / "input"
    solution_dir = task_root / "solution"
    for directory in (input_dir, solution_dir):
        _reset(directory)
    return input_dir, solution_dir


def _reset(directory: Path) -> None:
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True, exist_ok=True)


__all__ = [
    "MEASURED",
    "STRICT",
    "BuiltTask",
    "DEFAULT_REPEATS",
    "EXCISION",
    "HISTORY",
    "OVERLAY_FILES",
    "TaskBuildError",
    "build_evaluation_tree",
    "changed_test_functions",
    "collected_tests",
    "plan_excision",
    "screen_transition",
    "scope_files",
    "stage_excision_task",
    "stage_history_task",
    "validate_task",
]
