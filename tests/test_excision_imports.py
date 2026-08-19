"""Excising a Go body must leave a file that still compiles.

Removing a function body removes its references, and Go treats an import
nothing uses as a compile error. The candidate then reaches the fail-before
gate as a *build* failure — which the brief says does not count — so it is
correctly rejected and the repository yields nothing. Measured on a
table-driven fixture, this was the difference between one eligible excision
task and none.
"""

from __future__ import annotations

import pytest

from stress_stack.excision_multilang import excise_symbol

BODY = 'func ToType(v interface{}) string {\n\treturn fmt.Sprintf("%T", v)\n}\n'


def stub(code: str) -> str:
    result = excise_symbol("conv.go", code, "ToType")
    assert result is not None
    return result.stubbed


def test_a_grouped_import_loses_only_the_orphaned_spec() -> None:
    """The block survives; dropping `import (` itself orphans the rest."""
    stubbed = stub(
        "package conv\n\nimport (\n\t\"fmt\"\n\t\"strings\"\n)\n\n"
        "func Keep(s string) string { return strings.ToUpper(s) }\n\n" + BODY
    )

    assert '"strings"' in stubbed
    assert '"fmt"' not in stubbed
    assert "import (" in stubbed
    assert stubbed.count(")") >= 1


def test_a_grouped_import_with_nothing_left_is_removed_whole() -> None:
    """An empty `import ()` is legal but pointless; leaving it is untidy."""
    stubbed = stub("package conv\n\nimport (\n\t\"fmt\"\n)\n\n" + BODY)

    assert "import" not in stubbed
    assert "panic" in stubbed or "not implemented" in stubbed


def test_a_single_line_import_is_removed() -> None:
    stubbed = stub('package conv\n\nimport "fmt"\n\n' + BODY)

    assert "import" not in stubbed


def test_an_import_still_used_elsewhere_survives() -> None:
    """The prune is about what the excision orphaned, not about tidying."""
    stubbed = stub(
        'package conv\n\nimport "fmt"\n\n'
        "func Other() string { return fmt.Sprint(1) }\n\n" + BODY
    )

    assert '"fmt"' in stubbed


@pytest.mark.parametrize("spec", ['_ "embed"', '. "math"'])
def test_blank_and_dot_imports_are_never_pruned(spec: str) -> None:
    """Neither is used through a qualifier, so absence of one proves nothing."""
    stubbed = stub(f"package conv\n\nimport (\n\t{spec}\n\t\"fmt\"\n)\n\n" + BODY)

    assert spec in stubbed


def test_an_aliased_import_is_judged_by_its_alias() -> None:
    stubbed = stub(
        'package conv\n\nimport (\n\tf "fmt"\n\ts "strings"\n)\n\n'
        "func Keep(x string) string { return s.ToUpper(x) }\n\n"
        'func ToType(v interface{}) string {\n\treturn f.Sprintf("%T", v)\n}\n'
    )

    assert 's "strings"' in stubbed
    assert 'f "fmt"' not in stubbed


def test_other_ecosystems_are_left_alone() -> None:
    """An unused import is a warning in Rust, so removing it is an unasked edit."""
    code = "use std::fmt;\n\nfn to_type(v: i32) -> String {\n    format!(\"{}\", v)\n}\n"
    result = excise_symbol("conv.rs", code, "to_type")

    assert result is not None
    assert "use std::fmt;" in result.stubbed
