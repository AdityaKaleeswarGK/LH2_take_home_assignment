"""Task validation gates.

The brief requires a fail-before that fails *for the right reason*: "an
assertion about behavior. A failure caused by an import error, syntax error, or
missing file does not count." Two consequences drive this module:

* **"An AssertionError occurred somewhere" is not sufficient.** The designated
  tests must be the ones that failed, so every gate works against an explicit
  set of target test ids and reports exactly which ones misbehaved.
* **A collection error collapses the whole run.** pytest reports only the
  collection failure and never mentions the targets, so a missing target id is
  itself a rejection — whatever the underlying cause.

Failure classes are read from the JUnit report: the ``<error>`` tag marks
collection and fixture-setup problems, while ``<failure>`` marks a failure in
the test body, whose message begins with the exception class name. The ``type``
attribute is empty in practice and is not relied upon.
"""

from __future__ import annotations

from collections.abc import Callable

import re
import xml.etree.ElementTree as ElementTree
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_EXCEPTION = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_.]*)\s*:")
_FRAME = re.compile(r"^(?P<path>[^\s:][^:\n]*):(?P<line>\d+): in ", re.MULTILINE)

# These mean the code never loaded, so no behaviour was ever exercised. The
# brief excludes exactly this category: "an import error, syntax error, or
# missing file does not count".
_LOAD_FAILURES = frozenset(
    {
        "ImportError",
        "ModuleNotFoundError",
        "SyntaxError",
        "IndentationError",
        "CollectionError",
    }
)

ASSERTION = "assertion"
BEHAVIORAL_EXCEPTION = "behavioral_exception"
INFRASTRUCTURE = "infrastructure"
PASSED = "passed"
SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class CaseResult:
    test_id: str
    status: str
    failure_class: str
    signature: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_id": self.test_id,
            "status": self.status,
            "failure_class": self.failure_class,
            "signature": self.signature,
        }


@dataclass
class RunReport:
    results: dict[str, CaseResult] = field(default_factory=dict)
    collected: bool = True
    exit_code: int | None = None

    def status_of(self, test_id: str) -> str | None:
        result = self.results.get(test_id)
        return result.status if result else None

    def failing(self) -> set[str]:
        return {key for key, value in self.results.items() if value.status == "failed"}

    def passing(self) -> set[str]:
        return {key for key, value in self.results.items() if value.status == PASSED}

    def class_counts(self) -> dict[str, int]:
        return dict(Counter(value.failure_class for value in self.results.values()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "collected": self.collected,
            "exit_code": self.exit_code,
            "total": len(self.results),
            "class_counts": self.class_counts(),
            "results": {key: value.to_dict() for key, value in sorted(self.results.items())},
        }


@dataclass(frozen=True, slots=True)
class GateVerdict:
    gate: str
    passed: bool
    reason_code: str | None
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "passed": self.passed,
            "reason_code": self.reason_code,
            "detail": self.detail,
        }


def traceback_frames(traceback: str) -> list[str]:
    """File paths appearing as traceback frames, in order."""
    return [match.group("path") for match in _FRAME.finditer(traceback or "")]


_EXTERNAL_MARKERS = ("site-packages", "/lib/python", "dist-packages", "/usr/lib")


def reaches_repository_code(traceback: str, code_root: str | None = None) -> bool:
    """Whether execution actually entered the repository's own source.

    Relative-versus-absolute is not a safe signal. Stale ``__pycache__`` carries
    the absolute path of whichever machine compiled it, so the same test can
    render a relative frame in one run and an absolute foreign-looking one in
    the next. Frames are therefore judged by whether they sit outside a known
    interpreter or package directory, and — when the caller supplies one —
    whether they sit under the tree being tested.
    """
    for path in traceback_frames(traceback):
        if any(marker in path for marker in _EXTERNAL_MARKERS):
            continue
        if code_root and path.startswith("/") and code_root not in path:
            continue
        return True
    return False


def _is_assertion(message: str, traceback: str) -> bool:
    """pytest's terse output drops the ``AssertionError:`` prefix.

    A rewritten assertion surfaces as a bare ``assert x == y`` line, so the
    prefix alone is not a reliable test for "this was an assertion".
    """
    text = (message or "").strip()
    if text.startswith(("AssertionError", "assert ")):
        return True
    # Only pytest's `E ` lines carry the raised error. An `assert` appearing as a
    # plain frame line is merely where execution was, not what went wrong: glom's
    # pickle failure raises KeyError *while evaluating* an assert expression.
    for line in (traceback or "").splitlines():
        if not line.startswith("E "):
            continue
        if line[1:].strip().startswith(("assert ", "AssertionError")):
            return True
    return False


def classify(tag: str, message: str, traceback: str = "", code_root: str | None = None) -> str:
    """Which failure class a JUnit child element represents.

    Classification is by *origin*, not by exception name. A feature that does
    not exist yet usually fails by raising from inside the library rather than
    by tripping an ``assert`` — glom's pickle support raised ``KeyError`` from
    ``core.py`` before ``__getstate__`` existed. That is a behavioural failure
    and must qualify; only a failure to load the code disqualifies.
    """
    if tag == "error":
        return INFRASTRUCTURE
    match = _EXCEPTION.match(message or "")
    name = match.group(1).rsplit(".", 1)[-1] if match else ""
    if name in _LOAD_FAILURES:
        return INFRASTRUCTURE
    if _is_assertion(message, traceback):
        return ASSERTION
    if reaches_repository_code(traceback, code_root):
        return BEHAVIORAL_EXCEPTION
    return INFRASTRUCTURE if traceback else BEHAVIORAL_EXCEPTION


def normalize_signature(message: str) -> str:
    """A stable failure fingerprint: exception class plus first message line.

    Paths, addresses and line numbers are stripped so the same defect produces
    the same signature across containers and repeats.
    """
    first = (message or "").strip().splitlines()[0] if (message or "").strip() else ""
    first = re.sub(r"0x[0-9a-fA-F]+", "0xADDR", first)
    first = re.sub(r"(/[\w.\-]+)+/", "<path>/", first)
    first = re.sub(r"\bline \d+\b", "line N", first)
    return first[:200]


def parse_report(path: Path) -> RunReport:
    report = RunReport()
    if not path.exists():
        report.collected = False
        return report
    try:
        tree = ElementTree.parse(path)
    except ElementTree.ParseError:
        report.collected = False
        return report

    for case in tree.iter("testcase"):
        class_name = case.get("classname") or ""
        name = case.get("name") or "<unnamed>"
        test_id = f"{class_name}::{name}" if class_name else name
        status, failure_class, signature = PASSED, PASSED, ""
        for child in case:
            message = child.get("message") or (child.text or "")
            if child.tag == "skipped":
                status, failure_class = SKIPPED, SKIPPED
            elif child.tag in {"failure", "error"}:
                status = "failed"
                traceback = child.text or ""
                failure_class = classify(child.tag, message, traceback)
                signature = normalize_signature(message)
                if child.tag == "error" and not class_name:
                    report.collected = False
            break
        report.results[test_id] = CaseResult(test_id, status, failure_class, signature)
    return report


def nearest_collected_ancestor(test_id: str, collected: set[str]) -> str | None:
    """The closest enclosing test that a run actually reported, if any.

    Test ids nest, and the separator differs by runner: Go writes
    ``TestUintSlice/#06/Value/ToType``, pytest writes
    ``module.py::TestClass::test_case``. Both are walked from the leaf
    outwards, and the first ancestor present in the report wins. ``None`` means
    nothing in the chain was collected, which is a real answer and must stay
    distinguishable from "the leaf itself was fine".
    """
    for separator in ("/", "::"):
        parts = test_id.split(separator)
        for cut in range(len(parts) - 1, 0, -1):
            ancestor = separator.join(parts[:cut])
            if ancestor in collected:
                return ancestor
    return None


def resolve_targets(
    designated: list[str],
    reports: list[RunReport],
    *,
    selection_id: Callable[[str], str] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Turn designated tests into ids a targeted re-run can actually select.

    A designated test is chosen from two *unfiltered* runs and then re-run with
    the suite narrowed to it. Some tests do not exist in both runs, and Go's
    table-driven subtests are the case that broke an ecosystem.

    A subtest is generated at run time by the parent that spawns it. Excise a
    function the table exercises and the first case panics, which aborts the
    parent — so the remaining cases are never generated at all. Measured on a
    four-level table: the excised tree collects 4 ids where the reference tree
    collects 25. A target taken from the reference run therefore names something
    that cannot exist in the run it has to fail in, and ``gate_fail_before``
    reported ``targets_not_collected`` for twelve Go candidates out of thirteen.

    So the requirement is presence in **every** run being compared, not in any
    of them. A leaf missing from one side lifts to the closest ancestor both
    sides reported — here ``TestUintSlice``, which is exactly the test that does
    panic before and does pass after. Two properties keep this honest:

    * **It is measured, not predicted.** No ecosystem rule decides what lifts;
      the reports do. An id no run collected is left exactly as it was, so the
      gate still fails and still names it.
    * **It does not change pytest.** A parametrised case is collected under its
      own id in both runs, so it is its own answer and nothing lifts. The
      parametrised case stays the unit, which is what it should be.

    Returns the resolved ids and a record of every lift, because a gate verdict
    reached against a parent rather than the designated leaf is a weaker claim
    and the artifact has to say so.
    """
    # Intersection, deliberately. The union would say a target "exists" on the
    # strength of the run it passes in, which is the one run that was never in
    # question.
    collected: set[str] | None = None
    for report in reports:
        ids = set(report.results)
        collected = ids if collected is None else (collected & ids)
    collected = collected or set()

    resolved: list[str] = []
    lifted: dict[str, str] = {}
    unresolved: list[str] = []
    for target in designated:
        selectable = selection_id(target) if selection_id else target
        if selectable != target and selectable in collected:
            lifted[target] = selectable
            resolved.append(selectable)
            continue
        if target in collected:
            resolved.append(target)
            continue
        ancestor = nearest_collected_ancestor(target, collected)
        if ancestor is not None:
            lifted[target] = ancestor
            resolved.append(ancestor)
            continue
        # Nothing in this chain was collected. Keeping the original is the
        # honest move: the gate will reject it and name it.
        unresolved.append(target)
        resolved.append(target)

    ordered = sorted(dict.fromkeys(resolved))
    return ordered, {
        "designated": sorted(designated),
        "resolved": ordered,
        "lifted": dict(sorted(lifted.items())),
        "unresolved": sorted(unresolved),
        # More than one leaf collapsing onto the same ancestor means the gate
        # can no longer tell which of them the change actually moved. It is not
        # a rejection — the ancestor genuinely fails before and passes after —
        # but it is weaker evidence, and it is recorded rather than smoothed.
        "collapsed": sorted(
            ancestor
            for ancestor in set(lifted.values())
            if sum(1 for value in lifted.values() if value == ancestor) > 1
        ),
    }


def gate_fail_before(
    report: RunReport, targets: set[str], *, require_assertion: bool = False
) -> GateVerdict:
    """Designated tests must fail, and for a behavioural reason.

    Default policy accepts an assertion *or* an exception raised from inside the
    repository, because an absent feature commonly fails by raising rather than
    by tripping an assert. Set ``require_assertion`` for the stricter reading.
    """
    if not targets:
        return GateVerdict("fail_before", False, "no_targets_designated", {})
    if not report.collected:
        return GateVerdict("fail_before", False, "collection_failed", {})

    missing = sorted(targets - set(report.results))
    if missing:
        return GateVerdict("fail_before", False, "targets_not_collected", {"missing": missing})

    not_failing = sorted(t for t in targets if report.results[t].status != "failed")
    if not_failing:
        return GateVerdict(
            "fail_before", False, "targets_did_not_fail", {"still_passing": not_failing}
        )

    accepted = {ASSERTION} if require_assertion else {ASSERTION, BEHAVIORAL_EXCEPTION}
    wrong = sorted(t for t in targets if report.results[t].failure_class not in accepted)
    if wrong:
        return GateVerdict(
            "fail_before",
            False,
            "failed_for_wrong_reason",
            {"targets": {t: report.results[t].failure_class for t in wrong}},
        )
    return GateVerdict(
        "fail_before",
        True,
        None,
        {"targets": {t: report.results[t].signature for t in sorted(targets)}},
    )


def gate_pass_after(report: RunReport, targets: set[str]) -> GateVerdict:
    if not report.collected:
        return GateVerdict("pass_after", False, "collection_failed", {})
    missing = sorted(targets - set(report.results))
    if missing:
        return GateVerdict("pass_after", False, "targets_not_collected", {"missing": missing})
    not_passing = sorted(t for t in targets if report.results[t].status != PASSED)
    if not_passing:
        return GateVerdict("pass_after", False, "targets_did_not_pass", {"failing": not_passing})
    return GateVerdict("pass_after", True, None, {"targets": sorted(targets)})


def gate_collateral(
    baseline: RunReport, after: RunReport, *, ignore: set[str] | None = None
) -> GateVerdict:
    """No test that passed at this snapshot may fail under the reference solution.

    ``ignore`` exists for tests the change deliberately deleted. A removed test
    is absent from the reference run and would otherwise read as a regression,
    which would reject every pull request that retired an obsolete assertion.
    Callers must supply only ids they can show the change removed on purpose.
    """
    if not baseline.collected or not after.collected:
        return GateVerdict("collateral", False, "collection_failed", {})
    excluded = ignore or set()
    disappeared = sorted(set(baseline.results) - set(after.results) - excluded)
    if disappeared:
        return GateVerdict(
            "collateral",
            False,
            "tests_disappeared",
            {"tests": disappeared[:25]},
        )
    new_infrastructure = sorted(
        test_id
        for test_id, result in after.results.items()
        if result.failure_class == INFRASTRUCTURE
        and baseline.results.get(test_id, CaseResult(test_id, PASSED, PASSED, "")).failure_class
        != INFRASTRUCTURE
    )
    if new_infrastructure:
        return GateVerdict(
            "collateral",
            False,
            "new_infrastructure_failures",
            {"tests": new_infrastructure[:25]},
        )
    regressions = sorted(baseline.passing() - after.passing() - excluded)
    if regressions:
        return GateVerdict(
            "collateral", False, "new_failures", {"regressions": regressions[:25]}
        )
    return GateVerdict(
        "collateral",
        True,
        None,
        {
            "baseline_passing": len(baseline.passing()),
            "after_passing": len(after.passing()),
            "deliberately_removed": sorted(excluded & baseline.passing())[:25],
        },
    )


def gate_determinism(reports: list[RunReport], targets: set[str]) -> GateVerdict:
    """Repeated fresh runs must agree on ids, statuses and failure signatures."""
    if len(reports) < 2:
        return GateVerdict("determinism", False, "insufficient_repeats", {"runs": len(reports)})

    id_sets = [frozenset(report.results) for report in reports]
    if len(set(id_sets)) != 1:
        union = set().union(*id_sets)
        unstable = sorted(t for t in union if any(t not in s for s in id_sets))
        return GateVerdict(
            "determinism", False, "unstable_collection", {"unstable_ids": unstable[:25]}
        )

    unstable_status = sorted(
        test_id
        for test_id in id_sets[0]
        if len({report.results[test_id].status for report in reports}) != 1
    )
    if unstable_status:
        return GateVerdict(
            "determinism", False, "unstable_status", {"tests": unstable_status[:25]}
        )

    unstable_signature = sorted(
        test_id
        for test_id in targets & id_sets[0]
        if len({report.results[test_id].signature for report in reports}) != 1
    )
    if unstable_signature:
        return GateVerdict(
            "determinism", False, "unstable_signature", {"tests": unstable_signature[:25]}
        )
    return GateVerdict("determinism", True, None, {"repeats": len(reports)})


def gate_verifier_integrity(verifier_files: dict[str, str]) -> GateVerdict:
    """A verifier must not be able to read the answer instead of testing for it."""
    if not verifier_files:
        return GateVerdict(
            "verifier_integrity", False, "no_verifier_files", {"files_checked": 0}
        )
    patterns = {
        # Matches both `git show HEAD` and the list form `["git", "show", ...]`.
        "reads_git_history": re.compile(
            r"\bgit\b[\s,'\"\]\[]{0,8}(log|show|diff|rev-parse|blame|checkout)\b"
            r"|\bdulwich\b|\bpygit2\b"
        ),
        "reads_solution_path": re.compile(r"[\"'][^\"']*\b(solution|goldenSolution)\b[^\"']*[\"']"),
        "reads_diff_artifact": re.compile(r"\.(patch|diff)\b"),
        "inspects_own_source": re.compile(r"\binspect\.getsource\b|\b__file__\s*\)?\s*\.read"),
    }
    findings: dict[str, list[str]] = {}
    for path, content in sorted(verifier_files.items()):
        for name, pattern in patterns.items():
            if pattern.search(content):
                findings.setdefault(name, []).append(path)
    if findings:
        return GateVerdict("verifier_integrity", False, "verifier_can_read_answer", findings)
    return GateVerdict(
        "verifier_integrity", True, None, {"files_checked": len(verifier_files)}
    )


def summarize(verdicts: list[GateVerdict]) -> dict[str, Any]:
    """Roll up a set of verdicts.

    The key is ``all_gates_passed`` rather than ``eligible`` because those are
    not the same claim and conflating them published a false one: a candidate
    rejected before any gate ran has an empty verdict list, ``all()`` over which
    is vacuously true. Merged into a task record it overwrote the real answer,
    so the artifact reported a task with no designated tests as eligible while
    the command line correctly called it rejected. Eligibility needs the
    rejection reason too, and only the task knows that.
    """
    failed = [verdict for verdict in verdicts if not verdict.passed]
    return {
        "all_gates_passed": bool(verdicts) and not failed,
        "failed_gates": [verdict.gate for verdict in failed],
        "reason_codes": [verdict.reason_code for verdict in failed if verdict.reason_code],
        "gates": [verdict.to_dict() for verdict in verdicts],
    }
