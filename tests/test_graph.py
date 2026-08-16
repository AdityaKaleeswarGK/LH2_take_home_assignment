from __future__ import annotations

from pathlib import Path

from stress_stack.graph import blast_radius, build_graph, validate_graph


def write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def sample_repository(root: Path) -> None:
    write(root, "pkg/__init__.py", "from pkg.core import glom, Engine\n")
    write(
        root,
        "pkg/core.py",
        "class Base:\n"
        "    pass\n"
        "\n"
        "\n"
        "class Engine(Base):\n"
        "    def run(self):\n"
        "        return glom()\n"
        "\n"
        "\n"
        "def glom():\n"
        "    return len([])\n",
    )
    write(
        root,
        "pkg/api.py",
        "from pkg import glom\n"
        "\n"
        "\n"
        "def entry():\n"
        "    return glom()\n",
    )


def test_builds_contains_imports_inherits_and_call_edges(tmp_path: Path) -> None:
    sample_repository(tmp_path)
    graph = build_graph(tmp_path)
    kinds = {(edge.kind, edge.source, edge.target) for edge in graph.edges}

    assert ("contains", "pkg/core.py::pkg.core", "pkg/core.py::pkg.core.Engine") in kinds
    assert ("inherits", "pkg/core.py::pkg.core.Engine", "pkg/core.py::pkg.core.Base") in kinds
    assert ("imports", "pkg/api.py::pkg.api", "pkg/__init__.py::pkg") in kinds


def test_follows_reexports_through_package_init(tmp_path: Path) -> None:
    """pkg/api.py imports glom from pkg, which re-exports it from pkg.core."""
    sample_repository(tmp_path)
    graph = build_graph(tmp_path)

    calls = {
        (edge.source, edge.target)
        for edge in graph.edges
        if edge.kind == "calls"
    }
    assert ("pkg/api.py::pkg.api.entry", "pkg/core.py::pkg.core.glom") in calls


def test_builtin_calls_are_classified_not_reported_as_failures(tmp_path: Path) -> None:
    sample_repository(tmp_path)
    graph = build_graph(tmp_path)

    reasons = {item.expression: item.reason for item in graph.unresolved}
    assert reasons["len"] == "builtin"
    assert graph.statistics()["unresolved_builtin"] >= 1


def test_every_edge_and_anchor_reverifies_against_source(tmp_path: Path) -> None:
    sample_repository(tmp_path)
    graph = build_graph(tmp_path)

    report = validate_graph(graph, tmp_path)

    assert report["status"] == "verified"
    assert report["edge_match_rate"] == 1.0
    assert report["anchor_match_rate"] == 1.0
    assert report["edges_only_in_graph"] == []


def test_validation_detects_a_graph_that_no_longer_matches_source(tmp_path: Path) -> None:
    sample_repository(tmp_path)
    graph = build_graph(tmp_path)
    write(tmp_path, "pkg/api.py", "def entry():\n    return None\n")

    report = validate_graph(graph, tmp_path)

    assert report["status"] == "mismatched"
    assert report["edge_match_rate"] < 1.0


def test_blast_radius_reports_external_callers_only(tmp_path: Path) -> None:
    sample_repository(tmp_path)
    graph = build_graph(tmp_path)

    radius = blast_radius(graph, {"pkg/core.py"})

    assert radius["changed"] == ["pkg/core.py"]
    assert "pkg/core.py" in radius["impacted"]
    callers = {entry["caller"] for entry in radius["impacted"]["pkg/core.py"]}
    assert "pkg/api.py::pkg.api.entry" in callers
    assert all("pkg/core.py::" not in caller for caller in callers)


def test_blast_radius_of_a_leaf_is_empty(tmp_path: Path) -> None:
    sample_repository(tmp_path)
    graph = build_graph(tmp_path)

    radius = blast_radius(graph, {"pkg/api.py"})

    assert radius["impacted"] == {}
    assert radius["impacted_file_count"] == 0


def test_graph_serialises_deterministically(tmp_path: Path) -> None:
    sample_repository(tmp_path)

    first = build_graph(tmp_path).to_dict()
    second = build_graph(tmp_path).to_dict()

    assert first == second
    assert first["edges"] == sorted(
        first["edges"],
        key=lambda e: (e["kind"], e["source"], e["target"], e["anchor"]["path"], e["anchor"]["line"]),
    )


def test_src_layout_imports_resolve_as_internal(tmp_path: Path) -> None:
    write(tmp_path, "src/pkg/__init__.py", "from pkg.core import run\n")
    write(tmp_path, "src/pkg/core.py", "def run():\n    return 1\n")
    write(tmp_path, "src/pkg/api.py", "from pkg import run\n\n\ndef entry():\n    return run()\n")

    graph = build_graph(tmp_path)
    calls = {(e.source, e.target) for e in graph.edges if e.kind == "calls"}

    assert ("src/pkg/api.py::pkg.api.entry", "src/pkg/core.py::pkg.core.run") in calls


def relative_repository(root: Path) -> None:
    write(root, "src/zoo/__init__.py", "from zoo.core import run\n")
    write(root, "src/zoo/forms.py", "def use():\n    return 1\n")
    write(root, "src/zoo/core.py", "from . import forms\nfrom .forms import use\nfrom .deep import helper\n")
    write(root, "src/zoo/deep/__init__.py", "def helper():\n    return 2\n")
    write(root, "src/zoo/deep/nested/__init__.py", "")
    write(
        root,
        "src/zoo/deep/nested/leaf.py",
        "from ... import forms\nfrom ...forms import use\nfrom .. import helper\n",
    )


def test_relative_imports_resolve_at_every_level(tmp_path: Path) -> None:
    relative_repository(tmp_path)
    graph = build_graph(tmp_path)
    imports = {
        (e.anchor.path, e.anchor.line): e.target for e in graph.edges if e.kind == "imports"
    }

    assert imports[("src/zoo/core.py", 1)] == "src/zoo/forms.py::zoo.forms"
    assert imports[("src/zoo/core.py", 2)] == "src/zoo/forms.py::zoo.forms"
    assert imports[("src/zoo/core.py", 3)] == "src/zoo/deep/__init__.py::zoo.deep"
    assert imports[("src/zoo/deep/nested/leaf.py", 1)] == "src/zoo/forms.py::zoo.forms"
    assert imports[("src/zoo/deep/nested/leaf.py", 2)] == "src/zoo/forms.py::zoo.forms"
    assert imports[("src/zoo/deep/nested/leaf.py", 3)] == "src/zoo/deep/__init__.py::zoo.deep"


def test_dot_means_the_package_itself_inside_init(tmp_path: Path) -> None:
    """In pkg/__init__.py, `from .mod import x` stays inside pkg."""
    write(tmp_path, "pkg/__init__.py", "from .mod import thing\n")
    write(tmp_path, "pkg/mod.py", "thing = 1\n")

    graph = build_graph(tmp_path)
    imports = {e.target for e in graph.edges if e.kind == "imports"}

    assert imports == {"pkg/mod.py::pkg.mod"}


def test_stdlib_imports_are_not_reported_as_external(tmp_path: Path) -> None:
    write(tmp_path, "m.py", "import os\nimport json\nimport requests\n")
    graph = build_graph(tmp_path)

    assert graph.external_modules == ["requests"]
    reasons = {i.expression: i.reason for i in graph.unresolved if i.kind == "import"}
    assert reasons["import os"] == "stdlib_module"
    assert reasons["import requests"] == "external_module"
