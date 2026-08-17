from __future__ import annotations

import io

from stress_stack.progress import Console, Reporter, reporter, reporting


def test_the_default_reporter_is_silent() -> None:
    """A library caller and the test suite must see no output at all."""
    quiet = reporter()
    assert isinstance(quiet, Reporter)
    assert quiet.stage_start("mine") is None
    assert quiet.step("anything", 1, 2) is None
    assert quiet.stage_end("mine", "ok", 1.0) is None


def test_reporting_installs_and_restores() -> None:
    before = reporter()
    with reporting(Console(stream=io.StringIO())) as installed:
        assert reporter() is installed
    assert reporter() is before


def test_reporting_restores_even_when_the_body_raises() -> None:
    before = reporter()
    try:
        with reporting(Console(stream=io.StringIO())):
            raise RuntimeError("stage blew up")
    except RuntimeError:
        pass
    assert reporter() is before


def test_a_non_terminal_stream_gets_one_line_per_step() -> None:
    """Piped output must stay greppable — no carriage returns, no escapes."""
    stream = io.StringIO()
    console = Console(stream=stream, stage="validate")

    console.step("pr-117", 1, 3)
    console.step("pr-196", 2, 3)
    console.stage_end("validate", "ok", 145.4, "14 eligible")

    lines = stream.getvalue().splitlines()
    assert lines[0] == "  · validate 1/3 pr-117"
    assert lines[1] == "  · validate 2/3 pr-196"
    assert "validate" in lines[2] and "ok" in lines[2] and "145.4s" in lines[2]
    assert "\r" not in stream.getvalue()
    assert "\033" not in stream.getvalue()


def test_stage_end_marks_status_distinctly() -> None:
    stream = io.StringIO()
    console = Console(stream=stream)
    for status in ("ok", "degraded", "failed"):
        console.stage_end("s", status, 1.0)
    marks = [line[0] for line in stream.getvalue().splitlines()]

    assert len(set(marks)) == 3


def test_a_closed_stream_never_takes_the_run_down() -> None:
    stream = io.StringIO()
    stream.close()

    Console(stream=stream).step("still running", 1, 2)
