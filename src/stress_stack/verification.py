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
    excluded = ignore or set()
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
