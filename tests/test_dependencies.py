from __future__ import annotations

from pathlib import Path

from stress_stack.dependencies import (
    DependencyReport,
    build_report,
    classify_imports,
    render_lockfile,
)
from stress_stack.graph import build_graph


def write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def sample_repository(root: Path) -> None:
    write(root, "pkg/__init__.py", "")
    write(
        root,
        "pkg/core.py",
        "import os\n"
        "import json\n"
        "import attr\n"
        "from pkg import helpers\n"
        "\n"
        "try:\n"
        "    import yaml\n"
        "except ImportError:\n"
        "    yaml = None\n",
    )
    write(root, "pkg/helpers.py", "import sys\n")
    write(root, "tests/test_core.py", "import pytest\nimport attr\n")


def test_classifies_internal_stdlib_and_third_party(tmp_path: Path) -> None:
    sample_repository(tmp_path)
    facts = {fact.module: fact.kind for fact in classify_imports(build_graph(tmp_path))}

    assert facts["pkg"] == "internal"
    assert facts["os"] == "stdlib"
    assert facts["json"] == "stdlib"
    assert facts["sys"] == "stdlib"
    assert facts["attr"] == "third_party"
    assert facts["yaml"] == "third_party"
    assert facts["pytest"] == "third_party"


def test_guarded_import_is_optional_not_runtime(tmp_path: Path) -> None:
    sample_repository(tmp_path)
    facts = {fact.module: fact for fact in classify_imports(build_graph(tmp_path))}

    assert facts["yaml"].always_guarded is True
    assert facts["yaml"].guards == ["try_import"]
    assert facts["yaml"].bucket == "optional"


def test_import_used_outside_tests_is_runtime(tmp_path: Path) -> None:
    sample_repository(tmp_path)
    facts = {fact.module: fact for fact in classify_imports(build_graph(tmp_path))}

    assert facts["attr"].test_only is False
    assert facts["attr"].bucket == "runtime"
    assert facts["pytest"].test_only is True
    assert facts["pytest"].bucket == "test"


def test_anchors_point_at_the_importing_line(tmp_path: Path) -> None:
    sample_repository(tmp_path)
    facts = {fact.module: fact for fact in classify_imports(build_graph(tmp_path))}

    assert facts["yaml"].anchors == ["pkg/core.py:7"]


def test_report_buckets_pins_without_an_environment(tmp_path: Path) -> None:
    sample_repository(tmp_path)
    report = build_report(build_graph(tmp_path), python=None)

    assert report.environment_available is False
    assert report.runtime == {}
    assert set(report.unpinned) == {"attr", "yaml", "pytest"}


def test_audit_normalises_distribution_names() -> None:
    report = DependencyReport(
        facts=[],
        runtime={},
        test={},
        optional={},
        unpinned=[],
        declared=["PyYAML", "Face"],
        environment_available=True,
    )
    assert report.audit()["declared_not_imported"] == ["Face", "PyYAML"]

    report_with_import = DependencyReport(
        facts=classify_imports_stub(),
        runtime={},
        test={},
        optional={},
        unpinned=[],
        declared=["pyyaml"],
        environment_available=True,
    )
    audit = report_with_import.audit()
    assert audit["imported_not_declared"] == []
    assert audit["declared_not_imported"] == []


def classify_imports_stub():
    from stress_stack.dependencies import ImportFact

    return [ImportFact(module="yaml", kind="third_party", distribution="PyYAML")]


def test_lockfile_is_sorted_and_commented() -> None:
    content = render_lockfile({"face": "26.0.1", "attrs": "26.1.0"}, header="Header line.")

    assert content == "# Header line.\n\nattrs==26.1.0\nface==26.0.1\n"
