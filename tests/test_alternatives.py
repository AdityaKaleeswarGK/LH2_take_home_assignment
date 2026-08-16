"""Whether the verifier would accept an implementation other than the reference."""

from __future__ import annotations

from pathlib import Path

from stress_stack.alternatives import (
    private_symbols,
    rename_private,
    scan_coupling,
    verdict,
)

MODULE = '''\
def render(text):
    return _wrap(_escape(text))


def _escape(text):
    return text.replace("<", "&lt;")


def _wrap(text):
    return "<p>" + text + "</p>"


class _Cache:
    pass


def __dunder_left_alone():
    pass
'''


def test_private_module_level_symbols_are_found() -> None:
    names = private_symbols(MODULE)

    assert names == {"_escape", "_wrap", "_Cache"}
    assert "render" not in names
    assert "__dunder_left_alone" not in names


def test_renaming_rewrites_definitions_and_every_reference(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "render.py").write_text(MODULE, encoding="utf-8")
    (tmp_path / "pkg" / "other.py").write_text(
        "from pkg.render import _escape\n\n\ndef go(t):\n    return _escape(t)\n",
        encoding="utf-8",
    )

    mutation = rename_private(tmp_path, ["pkg/render.py"])

    rewritten = (tmp_path / "pkg" / "render.py").read_text()
    consumer = (tmp_path / "pkg" / "other.py").read_text()
    assert mutation.renames["_escape"] == "_escape_alt"
    assert "_escape_alt" in rewritten and "def _escape(" not in rewritten
    # The rename must follow across files or the tree stops importing.
    assert "_escape_alt" in consumer
    assert "render" in rewritten, "the public name must be untouched"
    assert set(mutation.files_changed) == {"pkg/render.py", "pkg/other.py"}


def test_a_module_with_no_private_symbols_yields_no_rename(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("def public(x):\n    return x\n", encoding="utf-8")

    mutation = rename_private(tmp_path, ["m.py"])

    assert mutation.renames == {}
    assert mutation.files_changed == []


def test_a_test_patching_an_internal_is_flagged() -> None:
    report = scan_coupling(
        {"tests/test_render.py": "from unittest import mock\n\nwith mock.patch('pkg.render._wrap'):\n    pass\n"},
        {"_wrap"},
    )

    assert report.patched_internals
    assert report.private_references


def test_an_exact_repr_assertion_is_flagged() -> None:
    """A correct alternative may format its repr differently."""
    report = scan_coupling({"t.py": "def test_x():\n    assert repr(thing) == '<Thing 1>'\n"}, set())

    assert report.exact_representation
    assert report.findings == 1


def test_a_behavioural_test_is_not_flagged() -> None:
    source = (
        "def test_render_escapes():\n"
        "    assert render('a < b') == '<p>a &lt; b</p>'\n"
    )
    report = scan_coupling({"t.py": source}, {"_wrap", "_escape"})

    assert report.findings == 0


def test_the_verdict_distinguishes_survival_from_having_nothing_to_test() -> None:
    from stress_stack.alternatives import CouplingReport, Mutation

    nothing = verdict(Mutation({}, []), True, CouplingReport(), ran=True)
    survived = verdict(Mutation({"_a": "_a_alt"}, ["m.py"]), True, CouplingReport(), ran=True)
    coupled = verdict(Mutation({"_a": "_a_alt"}, ["m.py"]), False, CouplingReport(), ran=True)

    assert nothing["status"] == "no_internals_to_rename" and nothing["portable"] is True
    assert survived["status"] == "survived" and survived["portable"] is True
    assert coupled["status"] == "verifier_depends_on_internals"
    assert coupled["portable"] is False


def test_a_check_that_did_not_run_is_not_reported_as_a_pass() -> None:
    from stress_stack.alternatives import CouplingReport, Mutation

    result = verdict(Mutation({}, []), False, CouplingReport(), ran=False)

    assert result["status"] == "not_run"
    assert result["portable"] is False
