from __future__ import annotations

from pathlib import Path

from stress_stack.coverage_map import CoverageMap, CoveredSymbol
from stress_stack.graph import build_graph
from stress_stack.testgen import (
    _mutate,
    _prune_nonpassing,
    _pythonpath,
    _test_problem,
    uncovered_targets,
)


def test_generated_test_requires_a_real_assertion() -> None:
    assert _test_problem("def test_x():\n    value = 1\n") == "test_x_has_no_assertion"
    assert _test_problem("def test_x():\n    assert value == 1\n") is None
    assert _test_problem(
        "import pytest\n\ndef test_x():\n    with pytest.raises(ValueError):\n        call()\n"
    ) is None


def test_uncovered_targets_only_select_public_production_callables(tmp_path: Path) -> None:
    (tmp_path / "pkg.py").write_text(
        "def public():\n    return 1\n\ndef _private():\n    return 2\n",
        encoding="utf-8",
    )
    graph = build_graph(tmp_path)
    symbols = {}
    for parsed in graph.files:
        for symbol in parsed.symbols:
            if symbol.kind == "module":
                continue
            symbols[symbol.id] = CoveredSymbol(
                symbol_id=symbol.id,
                path=parsed.path,
                kind=symbol.kind,
                qualified_name=symbol.qualified_name,
                body_lines=2,
            )
    coverage = CoverageMap("available", symbols=symbols)

    targets = uncovered_targets(graph, coverage)

    assert [target.qualified_name for target in targets] == ["pkg.public"]


def test_uncovered_targets_exclude_loose_docs_and_examples(tmp_path: Path) -> None:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "examples").mkdir()
    (tmp_path / "src" / "pkg" / "api.py").write_text("def run():\n    return 1\n")
    (tmp_path / "docs" / "conf.py").write_text("def configure():\n    return 1\n")
    (tmp_path / "examples" / "demo.py").write_text("def demo():\n    return 1\n")
    graph = build_graph(tmp_path)
    symbols = {
        symbol.id: CoveredSymbol(
            symbol_id=symbol.id,
            path=parsed.path,
            kind=symbol.kind,
            qualified_name=symbol.qualified_name,
            body_lines=2,
        )
        for parsed in graph.files
        for symbol in parsed.symbols
        if symbol.kind != "module"
    }

    targets = uncovered_targets(graph, CoverageMap("available", symbols=symbols), limit=5)

    assert [target.path for target in targets] == ["src/pkg/api.py"]


def test_mutation_uses_the_graph_anchor_not_every_same_named_method(tmp_path: Path) -> None:
    (tmp_path / "pkg.py").write_text(
        "class A:\n    def run(self):\n        return 'a'\n\n"
        "class B:\n    def run(self):\n        return 'b'\n",
        encoding="utf-8",
    )
    graph = build_graph(tmp_path)
    target_symbol = next(
        symbol
        for parsed in graph.files
        for symbol in parsed.symbols
        if symbol.qualified_name == "pkg.A.run"
    )
    target = CoveredSymbol(
        symbol_id=target_symbol.id,
        path="pkg.py",
        kind="method",
        qualified_name=target_symbol.qualified_name,
        body_lines=2,
    )

    changed = _mutate(tmp_path, [target], graph)
    source = (tmp_path / "pkg.py").read_text(encoding="utf-8")

    assert changed == ["pkg.py:run"]
    assert source.count("__stress_stack_mutation__") == 1
    assert "return 'b'" in source


def test_nonpassing_generated_functions_are_pruned_independently(tmp_path: Path) -> None:
    path = tmp_path / "test_generated.py"
    path.write_text(
        "def test_good():\n    assert 1 == 1\n\n"
        "def test_bad():\n    assert 1 == 2\n",
        encoding="utf-8",
    )

    _prune_nonpassing(
        [path],
        {"tests.test_generated::test_good": "passed", "tests.test_generated::test_bad": "failed"},
    )

    assert "test_good" in path.read_text(encoding="utf-8")
    assert "test_bad" not in path.read_text(encoding="utf-8")


def test_pythonpath_discovers_src_layout_inside_metadata_workdir(tmp_path: Path) -> None:
    root = tmp_path / ".stress_stack" / "work" / "mutant"
    package = root / "src" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")

    entries = _pythonpath(root).split(__import__("os").pathsep)

    assert str(root / "src") in entries
