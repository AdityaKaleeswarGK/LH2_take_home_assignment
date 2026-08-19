"""JavaScript and TypeScript, which needed a report reader and a place to write.

Neither was a grammar problem — tree-sitter already parsed both, prettier and
npm already worked. What blocked them was that `plan_for` returned None, so
validate reported the ecosystem unsupported rather than producing verdicts from
output nobody parsed.

The awkward parts are all about the sandbox rather than the language, and each
of these pins one of them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stress_stack.test_runners import parser_for, plan_for
from stress_stack.verification import ASSERTION, INFRASTRUCTURE, PASSED


def report(*cases: tuple[str, str, str], path: str = "/work/src/calc.test.ts") -> str:
    """A jest/vitest JSON report. vitest's reporter is jest-compatible."""
    return json.dumps(
        {
            "numTotalTests": len(cases),
            "success": all(status == "passed" for _, status, _ in cases),
            "testResults": [
                {
                    "name": path,
                    "status": "passed",
                    "message": "",
                    "assertionResults": [
                        {
                            "ancestorTitles": ["calc"],
                            "fullName": f"calc {title}",
                            "title": title,
                            "status": status,
                            "failureMessages": [message] if message else [],
                        }
                        for title, status, message in cases
                    ],
                }
            ],
        }
    )


def test_ids_are_relative_to_the_mount_not_absolute() -> None:
    """A verdict has to be re-checkable against a tree a grader lays out.

    The report names each file by absolute path, which inside the sandbox is
    under `/work`. An id carrying that prefix means nothing anywhere else.
    """
    run = plan_for("typescript").parse(report(("adds", "passed", "")), "", 0)

    assert list(run.results) == ["src/calc.test.ts::calc adds"]


def test_an_assertion_failure_is_not_infrastructure() -> None:
    """The fail-before gate needs a behavioural failure, not a broken runner."""
    run = plan_for("typescript").parse(
        report(("adds", "failed", "AssertionError: expected 12 to be 99")), "", 1
    )

    case = run.results["src/calc.test.ts::calc adds"]
    assert case.status == "failed"
    assert case.failure_class == ASSERTION


def test_a_module_that_will_not_resolve_is_infrastructure() -> None:
    run = plan_for("typescript").parse(
        report(("adds", "failed", "Error: Cannot find module './calc'")), "", 1
    )

    assert run.results["src/calc.test.ts::calc adds"].failure_class == INFRASTRUCTURE


def test_a_file_that_never_loaded_is_not_collected() -> None:
    """Nothing in it ran, which is not the same as nothing in it failing."""
    payload = json.dumps(
        {
            "numTotalTests": 0,
            "testResults": [
                {
                    "name": "/work/src/calc.test.ts",
                    "status": "failed",
                    "message": "Failed to resolve import './missing'",
                    "assertionResults": [],
                }
            ],
        }
    )

    run = plan_for("typescript").parse(payload, "", 1)

    assert not run.collected


def test_a_report_buried_in_banner_output_is_still_found() -> None:
    """npm prints its own preamble before the reporter's JSON."""
    noisy = "> calc@1.0.0 test\n> vitest run\n\n" + report(("adds", "passed", ""))

    run = plan_for("typescript").parse(noisy, "", 0)

    assert run.collected
    assert run.results["src/calc.test.ts::calc adds"].status == PASSED


def test_no_report_at_all_is_a_runner_that_never_started() -> None:
    run = plan_for("typescript").parse("", "EAI_AGAIN registry.npmjs.org", 1)

    assert not run.collected
    assert not run.results


def test_a_failing_suite_exit_code_is_a_result_not_a_crash() -> None:
    """jest and vitest exit 1 on test failure, the same convention as pytest."""
    assert 1 in plan_for("typescript").result_exit_codes
    assert 0 in plan_for("typescript").result_exit_codes


def test_targets_select_by_full_name_anchored() -> None:
    """`-t` is a regex over the full name, so an unanchored one over-selects."""
    command = plan_for("typescript").suite_command(
        ["src/calc.test.ts::calc adds", "src/calc.test.ts::calc multiplies"]
    )

    pattern = command[command.index("-t") + 1]
    assert pattern.startswith("^(") and pattern.endswith(")$")
    assert "calc\\ adds" in pattern


def test_the_suite_command_does_not_reach_for_the_network() -> None:
    """The sandbox has none; `npx` without a local install tries the registry."""
    command = plan_for("typescript").suite_command(None)

    assert "npx" not in command


def test_one_reader_serves_jest_and_vitest() -> None:
    assert parser_for("jest_json") is not None


@pytest.mark.parametrize("language", ["typescript", "javascript"])
def test_both_dialects_have_a_plan(language: str) -> None:
    assert plan_for(language) is not None


def test_node_needs_a_writable_place_to_put_its_cache(monkeypatch) -> None:
    """vitest derives its cache dir from Vite's, under node_modules, and
    `--cache.dir` is deprecated — so a read-only node_modules fails the run with
    an unhandled ENOENT *after* every test has already passed."""
    from stress_stack import runner as runner_module
    from stress_stack.runner import select_runner

    # The image check is correct and not what this measures.
    monkeypatch.setattr(runner_module, "_require_container", lambda image: None)
    runner = select_runner(image="stress-stack/x:verify", language="typescript")

    assert runner.policy.writable_subpaths == ("node_modules",)
    assert runner.environment()["NODE_PATH"] == "/deps/node_modules"


def test_a_writable_subpath_becomes_its_own_tmpfs(tmp_path: Path) -> None:
    """Layered over the read-only mount, so the snapshot stays untouchable."""
    from stress_stack.sandbox import SandboxPolicy, build_arguments

    arguments = build_arguments(
        "img",
        ["vitest", "run"],
        code_dir=tmp_path,
        evidence_dir=tmp_path,
        policy=SandboxPolicy(writable_subpaths=("node_modules",)),
    )

    assert any(a.startswith("--tmpfs=/work/node_modules:rw") for a in arguments)
    assert f"--volume={tmp_path.resolve()}:/work:ro" in arguments


def test_the_probed_command_beats_the_plans_default(monkeypatch) -> None:
    """The probe ran it against this repository; the default is a guess."""
    from stress_stack import runner as runner_module
    from stress_stack.runner import select_runner

    monkeypatch.setattr(runner_module, "_require_container", lambda image: None)
    runner = select_runner(
        image="stress-stack/x:verify",
        language="typescript",
        suite_command=("npx", "--no-install", "jest", "--json"),
    )

    assert runner.plan.suite_command(None) == ["npx", "--no-install", "jest", "--json"]


def test_an_npm_script_is_run_not_passed_back_to_itself() -> None:
    """`npm run test -- <body>` re-passes the script's body as arguments.

    A `"test": "vitest run"` script became `vitest run vitest run`, whose extra
    words are read as filename filters — matching nothing, exiting non-zero, and
    failing the container stage for a suite that passes.
    """
    from stress_stack.ci_parser import parse_ci_facts

    root = Path(__file__).parent / "_npm_fixture"
    root.mkdir(exist_ok=True)
    (root / "package.json").write_text(
        json.dumps({"name": "x", "scripts": {"test": "vitest run"}}), encoding="utf-8"
    )
    try:
        facts = parse_ci_facts(root)
    finally:
        (root / "package.json").unlink()
        root.rmdir()

    assert facts.test_commands == ["npm run test"]
