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


def test_renaming_can_be_limited_to_newly_introduced_private_symbols(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "def _old():\n    return 1\n\ndef _new():\n    return _old()\n",
        encoding="utf-8",
    )

    mutation = rename_private(tmp_path, ["m.py"], names={"_new"})

    rewritten = (tmp_path / "m.py").read_text(encoding="utf-8")
    assert mutation.renames == {"_new": "_new_alt"}
    assert "def _old()" in rewritten
    assert "def _new_alt()" in rewritten


def test_renaming_never_rewrites_the_verifier(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("def _secret():\n    return 1\n")
    verifier = tmp_path / "tests" / "test_mod.py"
    verifier.write_text("from pkg.mod import _secret\n\ndef test_x():\n    assert _secret() == 1\n")

    rename_private(tmp_path, ["pkg/mod.py"])

    assert "_secret_alt" in (tmp_path / "pkg" / "mod.py").read_text()
    assert "_secret_alt" not in verifier.read_text()


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

    # A static finding is recorded, never decisive. glom asserts on repr()
    # throughout — spec display is the library's subject — so one candidate
    # scored 62 findings while its mutation survived cleanly. Gating on the
    # scan rejected every task in the repository.
    statically_coupled = verdict(
        Mutation({"_a": "_a_alt"}, ["m.py"]),
        True,
        CouplingReport(exact_representation=["t.py:1"]),
        ran=True,
    )
    assert statically_coupled["status"] == "survived"
    assert statically_coupled["portable"] is True
    assert statically_coupled["coupling_findings"] == 1

    # The mutation still decides against it when the rename actually breaks it.
    really_coupled = verdict(
        Mutation({"_a": "_a_alt"}, ["m.py"]),
        False,
        CouplingReport(exact_representation=["t.py:1"]),
        ran=True,
    )
    assert really_coupled["portable"] is False


def test_a_check_that_did_not_run_is_not_reported_as_a_pass() -> None:
    from stress_stack.alternatives import CouplingReport, Mutation

    result = verdict(Mutation({}, []), False, CouplingReport(), ran=False)

    assert result["status"] == "not_run"
    assert result["portable"] is False
