"""Both graph builders answer the same questions, or neither is usable.

`blast_radius`, `scope_files` and `run_selection` are written once and run over
whichever graph the ecosystem produced. They were only ever exercised against
the Python graph, so the tree-sitter graph drifted: its edges were 3-tuples and
its symbols had no `.id`, which meant every non-Python task shipped a scope
listing only the files the solver had already been given.

These run the same assertions over both builders, so the two cannot drift again
without a test saying so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stress_stack.graph import blast_radius, build_graph, load_graph
from stress_stack.graph_multilang import build_graph as build_multilang
from stress_stack.tasks import scope_files


@pytest.fixture
def python_repository(tmp_path: Path) -> Path:
    # Its own directory. Both fixtures sharing `tmp_path` puts a go.mod beside
    # the .py files, and the detector then calls the Python repository Go.
    tmp_path = tmp_path / "py"
    tmp_path.mkdir()
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "calc"\n')
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "use.py").write_text("from calc import add\n\n\ndef use():\n    return add(1, 2)\n")
    (tmp_path / "other.py").write_text("def nothing():\n    return None\n")
    return tmp_path


@pytest.fixture
def go_repository(tmp_path: Path) -> Path:
    tmp_path = tmp_path / "go"
    tmp_path.mkdir()
    (tmp_path / "go.mod").write_text("module example.com/m\n\ngo 1.21\n")
    (tmp_path / "calc.go").write_text("package m\n\nfunc Add(a, b int) int {\n\treturn a + b\n}\n")
    (tmp_path / "use.go").write_text("package m\n\nfunc Use() int {\n\treturn Add(1, 2)\n}\n")
    (tmp_path / "other.go").write_text("package m\n\nfunc Nothing() {}\n")
    return tmp_path


def graphs(python_repository: Path, go_repository: Path) -> list[tuple[str, object, str]]:
    return [
        ("python", build_graph(python_repository), "calc.py"),
        ("tree_sitter", build_multilang(go_repository), "calc.go"),
    ]


def test_every_symbol_answers_id(python_repository: Path, go_repository: Path) -> None:
    for name, graph, _ in graphs(python_repository, go_repository):
        symbols = [s for parsed in graph.files for s in parsed.symbols]
        assert symbols, f"{name} produced no symbols"
        for symbol in symbols:
            assert symbol.id, f"{name}: a symbol has no id"
            assert "::" in symbol.id, f"{name}: {symbol.id} is not path::name"


def test_every_edge_answers_kind_source_target_and_anchor(
    python_repository: Path, go_repository: Path
) -> None:
    for name, graph, _ in graphs(python_repository, go_repository):
        assert graph.edges, f"{name} produced no edges"
        for edge in graph.edges:
            assert isinstance(edge.kind, str) and edge.kind
            assert isinstance(edge.source, str) and edge.source
            assert isinstance(edge.target, str) and edge.target
            # `str(anchor)` is what blast_radius records, so it has to be one.
            assert ":" in str(edge.anchor), f"{name}: anchor {edge.anchor!r} is not path:line"


def test_symbol_index_is_keyed_by_id(python_repository: Path, go_repository: Path) -> None:
    for name, graph, _ in graphs(python_repository, go_repository):
        index = graph.symbol_index()
        assert index, f"{name} produced an empty symbol index"
        for key, symbol in index.items():
            assert key == symbol.id, f"{name}: index key {key} does not match {symbol.id}"


def test_blast_radius_finds_the_caller_in_both_graphs(
    python_repository: Path, go_repository: Path
) -> None:
    """The point of the protocol: one function, two graphs, the same answer."""
    for name, graph, changed in graphs(python_repository, go_repository):
        radius = blast_radius(graph, {changed})
        callers = {
            entry["caller_path"]
            for entries in radius["impacted"].values()
            for entry in entries
        }
        expected = "use.py" if name == "python" else "use.go"
        assert expected in callers, f"{name}: blast_radius missed {expected}"


def test_scope_files_names_more_than_the_change(
    python_repository: Path, go_repository: Path
) -> None:
    """A scope listing only what the solver was handed is not a scope."""
    for name, graph, changed in graphs(python_repository, go_repository):
        scope = scope_files(graph, [changed])
        assert changed in scope
        assert len(scope) > 1, f"{name}: scope degenerated to the changed set"


def test_load_graph_dispatches_on_the_detected_ecosystem(
    python_repository: Path, go_repository: Path
) -> None:
    assert type(load_graph(python_repository)).__name__ == "RepositoryGraph"
    assert type(load_graph(go_repository)).__name__ == "MultiLanguageGraph"
