"""Per-language test execution, reported in the vocabulary the gates already use.

Every validation gate — fail-before, pass-after, collateral, determinism — is
written against ``verification.RunReport``: a per-test map of outcome, failure
class and signature. Nothing in those gates is Python-specific. What *was*
Python-specific is the only way to produce a ``RunReport``: parsing pytest's
JUnit XML.

So this module does not reimplement the gates. It produces a ``RunReport`` from
each ecosystem's own machine-readable test output, and the existing gates apply
unchanged.

The failure classification matters as much as the pass/fail. §5.4 requires a
task's fail-before to be "for the right reason": an assertion about behaviour,
not a compile error or a missing file. Each parser therefore has to distinguish
a test that ran and disagreed from a package that never built — which is why
these read structured output rather than grepping for "FAIL".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

from stress_stack.verification import (
    ASSERTION,
    BEHAVIORAL_EXCEPTION,
    INFRASTRUCTURE,
    PASSED,
    CaseResult,
    RunReport,
    normalize_signature,
)


class LanguageTestPlan(Protocol):
    language: str

    def suite_command(self, targets: list[str] | None) -> list[str]:
        """Command to run the suite, restricted to `targets` when given."""
        ...

    def parse(self, stdout: str, stderr: str, exit_code: int) -> RunReport:
        """Turn the runner's output into per-test results."""
        ...

    def selection_id(self, test_id: str) -> str:
        """The id this runner can actually select on a command line.

        Usually the id itself. It is not, wherever a runner reports a test
        under a name it cannot take back as a filter — see ``GoTestPlan``.
        """
        ...


# ------------------------------------------------------------------------- go

# `go test` reports a build failure as an output line on the package, not as a
# test result. Detecting it is what separates "the code is wrong" from "the code
# does not compile" — only the first can satisfy a fail-before gate.
_GO_BUILD_FAILURE = re.compile(
    r"(build failed|cannot find package|undefined:|syntax error|no required module)"
)


@dataclass
class GoTestPlan:
    language: str = "go"
    # 0 all passed, 1 some failed. 2 is a build or vet error, which is the
    # harness failing to run the suite rather than the suite reporting.
    result_exit_codes: frozenset[int] = frozenset({0, 1})

    def selection_id(self, test_id: str) -> str:
        """Every id `go test` reports, it can also select.

        An auto-generated subtest segment like `#06` looked unselectable and is
        not: `-run` takes it back fine once each path element is anchored
        separately. What actually breaks a Go excision task is that the subtest
        does not *exist* in the excised run — the parent panics on the first
        case and never spawns the rest — and that is settled against the reports
        in `verification.resolve_targets` rather than guessed at here.
        """
        return test_id

    def suite_command(self, targets: list[str] | None) -> list[str]:
        command = ["go", "test", "-json", "-count=1"]
        if targets:
            # Go selects by regex over test names, anchored so `TestAdd` does not
            # also run `TestAddMany`. A subtest id is a path, and `-run` matches
            # it element by element, so each element is anchored separately —
            # anchoring the whole string makes `TestX/sub` match nothing.
            names = "|".join(
                "/".join(f"^{re.escape(part)}$" for part in _test_name(t).split("/"))
                for t in targets
            )
            command += ["-run", names]
        return command + ["./..."]

    def parse(self, stdout: str, stderr: str, exit_code: int) -> RunReport:
        report = RunReport(exit_code=exit_code)
        output: dict[str, list[str]] = {}
        statuses: dict[str, str] = {}
        build_failed = _GO_BUILD_FAILURE.search(stderr) is not None
        saw_event = False

        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                # Build errors are emitted as bare text on stdout too.
                if _GO_BUILD_FAILURE.search(line):
                    build_failed = True
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            saw_event = True
            test = event.get("Test")
            if not test:
                if event.get("Action") == "output" and _GO_BUILD_FAILURE.search(
                    str(event.get("Output", ""))
                ):
                    build_failed = True
                continue
            key = f"{event.get('Package', '')}::{test}"
            action = event.get("Action")
            if action == "output":
                output.setdefault(key, []).append(str(event.get("Output", "")))
            elif action in {"pass", "fail", "skip"}:
                statuses[key] = {"pass": PASSED, "fail": "failed", "skip": "skipped"}[action]

        for key, status in statuses.items():
            text = "".join(output.get(key, []))
            report.results[key] = CaseResult(
                test_id=key,
                status=status,
                failure_class="" if status == PASSED else _go_failure_class(text),
                signature="" if status == PASSED else normalize_signature(_go_message(text)),
            )

        # No events at all, or a build failure, means nothing was collected.
        report.collected = saw_event and not build_failed and bool(statuses)
        return report


def _go_message(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith(("=== RUN", "--- FAIL", "--- PASS", "=== PAUSE")):
            return stripped
    return text.strip()[:200]


def _go_failure_class(text: str) -> str:
    lowered = text.lower()
    if _GO_BUILD_FAILURE.search(lowered):
        return INFRASTRUCTURE
    # `t.Fatal`/`t.Error` with a comparison is Go's assertion idiom; a panic that
    # reaches repository code is a behavioural failure, same as Python's.
    if "panic:" in lowered:
        return BEHAVIORAL_EXCEPTION
    if any(marker in lowered for marker in ("want", "expected", "got", "assert", "mismatch")):
        return ASSERTION
    return BEHAVIORAL_EXCEPTION


# ----------------------------------------------------------------------- rust

# libtest's stable text output: `test path::to::name ... ok`
_RUST_CASE = re.compile(r"^test\s+(?P<name>\S+)\s+\.\.\.\s+(?P<outcome>ok|FAILED|ignored)")
_RUST_FAILURE_BLOCK = re.compile(r"^---- (?P<name>\S+) stdout ----$")
_RUST_BUILD_FAILURE = re.compile(r"^error(\[E\d+\])?:", re.MULTILINE)


@dataclass
class RustTestPlan:
    language: str = "rust"
    # 101 is what `cargo test` exits when a test fails or panics — which is
    # exactly what a correct fail-before looks like. Reading it as pytest's
    # "anything but 0, 1, 5 is broken" threw away every Rust task.
    result_exit_codes: frozenset[int] = frozenset({0, 101})

    def selection_id(self, test_id: str) -> str:
        """libtest reports every test under a name `--exact` accepts back."""
        return test_id

    def suite_command(self, targets: list[str] | None) -> list[str]:
        command = ["cargo", "test", "--no-fail-fast"]
        if targets:
            # cargo takes a single filter substring; the exact set is narrowed
            # again when results are read back.
            command += ["--", *[_test_name(t) for t in targets]]
        return command

    def parse(self, stdout: str, stderr: str, exit_code: int) -> RunReport:
        report = RunReport(exit_code=exit_code)
        combined = stdout + "\n" + stderr
        # A compilation error is reported by cargo, not by libtest, and means no
        # test ever ran.
        if _RUST_BUILD_FAILURE.search(stderr) and "test result:" not in combined:
            report.collected = False
            return report

        messages: dict[str, list[str]] = {}
        current: str | None = None
        for line in combined.splitlines():
            block = _RUST_FAILURE_BLOCK.match(line.strip())
            if block:
                current = block.group("name") or None
                if current is not None:
                    messages[current] = []
                continue
            if current is not None:
                if line.startswith("----") or line.startswith("failures:"):
                    current = None
                else:
                    messages[current].append(line)

        for line in combined.splitlines():
            match = _RUST_CASE.match(line.strip())
            if not match:
                continue
            name = match.group("name")
            outcome = match.group("outcome")
            status = {"ok": PASSED, "FAILED": "failed", "ignored": "skipped"}[outcome]
            text = "\n".join(messages.get(name, []))
            report.results[name] = CaseResult(
                test_id=name,
                status=status,
                failure_class="" if status == PASSED else _rust_failure_class(text),
                signature="" if status == PASSED else normalize_signature(text),
            )

        report.collected = bool(report.results)
        return report


def _rust_failure_class(text: str) -> str:
    lowered = text.lower()
    if "assertion" in lowered or "assert_eq" in lowered or "assert!" in lowered:
        return ASSERTION
    if "panicked at" in lowered:
        return BEHAVIORAL_EXCEPTION
    return BEHAVIORAL_EXCEPTION if text.strip() else INFRASTRUCTURE


# --------------------------------------------------------------------- lookup

_PLANS: dict[str, Any] = {
    "go": GoTestPlan(),
    "rust": RustTestPlan(),
}


def _test_name(target: str) -> str:
    """The runner-visible test name from a fully qualified target id."""
    return target.rsplit("::", 1)[-1]


# Report formats, named so a workflow record can select one without being able
# to invent it. The keys are the vocabulary `workflow.REPORT_FORMATS` offers an
# agent; the values are parsers that already exist and are already tested.
_PARSERS: dict[str, Any] = {
    "go_json": lambda out, err, code: GoTestPlan().parse(out, err, code),
    "libtest": lambda out, err, code: RustTestPlan().parse(out, err, code),
}


def parser_for(report_format: str) -> Any | None:
    """The parser for a named report format, or None when there is not one.

    ``junit_xml`` is absent on purpose: it is read from a file the run writes,
    not from stdout, so it has a different signature and
    :func:`stress_stack.verification.parse_report` is its reader.
    """
    return _PARSERS.get(report_format)


def plan_for(language: str) -> Any | None:
    """The test plan for a language, or None when the gates cannot run there.

    JavaScript, TypeScript and C++ are absent deliberately rather than
    half-supported: each needs a reporter or harness that the repository has to
    already provide (`vitest --reporter=json`, `jest --json`, a CTest build
    directory). Returning None makes the validate stage report the ecosystem as
    unsupported instead of producing gate verdicts from unparsed output.
    """
    return _PLANS.get(language)


def supported_languages() -> frozenset[str]:
    return frozenset(_PLANS)
