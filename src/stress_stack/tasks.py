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
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stress_stack.candidates import EXCISION, HISTORY, Candidate
from stress_stack.errors import GitError, StressStackError
from stress_stack.excision import EXPLICIT, ExcisionError, excise, plan_excision
from stress_stack.git_repository import GitRepository
from stress_stack.graph import RepositoryGraph, blast_radius
from stress_stack.runner import RunOutcome, Runner, pytest_argument
from stress_stack.snapshot import (
    OVERLAY_FILES,
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
    GateVerdict,
    RunReport,
    gate_collateral,
    gate_determinism,
    gate_fail_before,
    gate_pass_after,
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
            "eligible": self.eligible,
            "rejected": self.rejected,
            "targets": self.targets,
            "verifier_files": self.verifier_files,
            "files_in_scope": self.files_in_scope,
            "detail": self.detail,
            "runs": [outcome.to_dict() for outcome in self.outcomes],
            **summarize(self.gates),
        }


# --------------------------------------------------------------------------
# Discovering what a change's tests actually are
# --------------------------------------------------------------------------


def collected_tests(source: str) -> dict[str, str]:
    """pytest-collectable test functions, keyed by their dotted name in the module.

    Mirrors pytest's defaults — ``test_*`` functions, and ``test_*`` methods on
    ``Test*`` classes that define no ``__init__``. The value is the unparsed
    body, so "this test changed" is a comparison rather than a guess.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}

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
    return found


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


def resolve_targets(
    tree: Path, designated: dict[str, list[str]], observed: set[str]
) -> tuple[list[str], list[str]]:
    """Match discovered test functions to the node ids the runner actually reported.

    The dotted ``classname`` pytest writes into JUnit depends on packaging, root
    directory and configuration. Rather than reconstruct it, every observed id
    is translated back into a path form and looked up — measured both ways.
    """
    by_argument = {pytest_argument(tree, node_id): node_id for node_id in observed}
    resolved: list[str] = []
    missing: list[str] = []
    for path, names in sorted(designated.items()):
        for name in names:
            argument = "::".join([path, *name.split(".")])
            node_id = by_argument.get(argument)
            if node_id is None:
                missing.append(argument)
            else:
                resolved.append(node_id)
    return sorted(set(resolved)), sorted(set(missing))


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

    designated = changed_test_functions(
        repository, transition, [str(p) for p in candidate.signals["test_paths"]]
    )
    if not designated:
        raise TaskBuildError("the change added or rewrote no test function")

    verifier_root = task_root / "verifier"
    _reset(verifier_root)
    for path in designated:
        content = show(repository, transition.head_sha, path)
        target = verifier_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    (task_root / "goldenSolution.diff").write_text(
        golden_diff(repository, transition), encoding="utf-8"
    )
    return transition, designated


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

    path = str(candidate.signals["path"])
    qualified = str(candidate.signals["qualified_name"])
    module = _module_prefix(qualified, path)
    source_file = input_dir / path
    if not source_file.is_file():
        raise TaskBuildError(f"{path} is not present in the materialised tree")

    original = source_file.read_text(encoding="utf-8")
    dotted = qualified[len(module) + 1 :] if module else qualified
    name = qualified.rsplit(".", 1)[-1]
    try:
        # Ask the plan which stubs are meaningful before honouring a preference:
        # a generator has no neutral body, and requesting one anyway would build
        # a task whose failure says nothing about the contract.
        plan = plan_excision(original, name, path=path, dotted=dotted)
        chosen = strategy if strategy in plan.strategies() else plan.strategies()[0]
        result = excise(original, name, strategy=chosen, path=path, dotted=dotted)
    except ExcisionError as exc:
        raise TaskBuildError(str(exc)) from exc

    source_file.write_text(result.stubbed, encoding="utf-8")
    (task_root / "goldenSolution.diff").write_text(result.diff(label=path), encoding="utf-8")

    covering = [str(t) for t in candidate.signals["covering_tests"]]
    designated = _group_by_file(covering, solution_dir)
    verifier_root = task_root / "verifier"
    _reset(verifier_root)
    for test_path in designated:
        target = verifier_root / test_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(solution_dir / test_path, target)

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
    return destination


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def validate_task(
    built: BuiltTask,
    runner: Runner,
    *,
    evaluation_tree: Path,
    repeats: int = DEFAULT_REPEATS,
) -> BuiltTask:
    """Run every gate and record both the verdicts and the runs behind them."""
    evidence = built.task_root / "evidence"
    solution_dir = built.task_root / "solution"

    after_full = runner.execute(solution_dir, evidence, "after_full")
    built.outcomes.append(after_full)
    if not after_full.usable:
        built.rejected = f"reference_run_failed: {after_full.infrastructure_failure}"
        return built

    observed = set(after_full.report.results)
    considered, missing = resolve_targets(
        solution_dir, built.detail.get("designated", {}), observed
    )
    built.detail["considered_tests"] = considered
    built.detail["unresolved_targets"] = missing
    if not considered:
        built.rejected = "no_target_test_resolved"
        return built

    # Collateral is a question about the *unmodified* pre-change tree: did the
    # reference solution break something that was passing before? Measuring it
    # on the evaluation tree was wrong — that tree carries the post-change test
    # files, so a verifier that cannot even import yet destroys the baseline
    # along with the fail-before evidence, and two unrelated questions fail
    # together.
    baseline_full = runner.execute(built.task_root / "input", evidence, "baseline_full")
    built.outcomes.append(baseline_full)
    if not baseline_full.usable:
        built.rejected = f"baseline_run_failed: {baseline_full.infrastructure_failure}"
        return built

    before_full = runner.execute(evaluation_tree, evidence, "before_full")
    built.outcomes.append(before_full)
    if not before_full.usable:
        built.rejected = f"fail_before_run_failed: {before_full.infrastructure_failure}"
        built.detail["fail_before_detail"] = before_full.detail
        return built

    # Designate by measurement, not by assumption. A change touches tests for
    # reasons that have nothing to do with behaviour — glom's Python 3.12 pull
    # request rewrote `class X(object):` to `class X:` and `set([])` to `set()`
    # across two dozen tests, all of which pass against the old source. Treating
    # every touched test as a target would sink genuine candidates on one
    # cosmetic edit, and would credit a modernisation sweep as a task.
    #
    # So the designated set is exactly the touched tests that actually fail for
    # a behavioural reason. The rest are not discarded: they stay in the full
    # before/after comparison, where the collateral gate still requires them to
    # keep passing.
    resolved = sorted(
        test_id
        for test_id in considered
        if _fails_behaviourally(before_full.report, test_id)
    )
    built.targets = resolved
    built.detail["considered_not_designated"] = sorted(set(considered) - set(resolved))
    if not resolved:
        built.rejected = "no_touched_test_fails_before"
        return built

    before_targets: list[RunOutcome] = []
    after_targets: list[RunOutcome] = []
    for index in range(1, max(2, repeats) + 1):
        before_targets.append(
            runner.execute(evaluation_tree, evidence, f"before_targets_{index}", resolved)
        )
        after_targets.append(
            runner.execute(solution_dir, evidence, f"after_targets_{index}", resolved)
        )
    built.outcomes.extend(before_targets + after_targets)

    broken = [o for o in before_targets + after_targets if not o.usable]
    if broken:
        built.rejected = f"verifier_run_failed: {broken[0].infrastructure_failure}"
        return built

    targets = set(resolved)
    built.gates = [
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
    ]
    built.detail["repeats"] = len(before_targets)
    built.detail["failure_classes"] = {
        target: before_targets[0].report.results[target].failure_class
        for target in sorted(targets)
        if target in before_targets[0].report.results
    }
    return built


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
    rewritten = {
        pytest_argument(built.task_root / "input", test_id).split("::", 1)[0]
        for test_id in vanished
    } & set(built.verifier_files)
    return {
        test_id
        for test_id in vanished
        if pytest_argument(built.task_root / "input", test_id).split("::", 1)[0] in rewritten
    }


def _fails_behaviourally(report: RunReport, test_id: str) -> bool:
    """Whether this test failed, and for a reason the brief accepts.

    An import error, a collection error or a syntax error means the code never
    loaded, so no behaviour was ever exercised — the brief excludes exactly that
    category, and a test that failed that way must not be designated.
    """
    result = report.results.get(test_id)
    return (
        result is not None
        and result.status == "failed"
        and result.failure_class in {ASSERTION, BEHAVIORAL_EXCEPTION}
    )


def scope_files(
    graph: RepositoryGraph, changed: list[str], *, limit: int = 25
) -> list[str]:
    """The files a solver plausibly has to read: what changed, plus what calls it."""
    radius = blast_radius(graph, set(changed))
    ordered = sorted(set(changed)) + [p for p in radius["impacted"] if p not in set(changed)]
    return ordered[:limit]


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
        for path in sorted(verifier_root.rglob("*.py"))
        if path.is_file()
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
    "resolve_targets",
    "scope_files",
    "stage_excision_task",
    "stage_history_task",
    "validate_task",
]
