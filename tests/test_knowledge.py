from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import run_git

from inverse_alpha.cli import main
from inverse_alpha.errors import InputError
from inverse_alpha.knowledge import build_knowledge


@pytest.fixture
def python_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "python-repository"
    (repository / "src" / "sample").mkdir(parents=True)
    (repository / "src" / "namespace").mkdir(parents=True)
    (repository / "tests").mkdir()
    (repository / "vendor").mkdir()
    run_git(repository, "init", "-b", "main")
    run_git(repository, "config", "user.name", "Inverse Alpha Test")
    run_git(repository, "config", "user.email", "inverse-alpha@example.test")

    (repository / "pyproject.toml").write_text(
        """
[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]
""".lstrip(),
        encoding="utf-8",
    )
    (repository / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (repository / "src" / "sample" / "__init__.py").write_text(
        "from .service import Worker\n\n__all__ = ['Worker']\n",
        encoding="utf-8",
    )
    (repository / "src" / "sample" / "base.py").write_text(
        "class Base:\n    pass\n",
        encoding="utf-8",
    )
    (repository / "src" / "sample" / "service.py").write_text(
        """
import json as json_module
from .base import Base


def trace(value):
    return value


def helper(value):
    return json_module.dumps(value)


@trace
class Worker(Base):
    async def run(self, value):
        def nested():
            return helper(value)

        self.normalize()
        return nested()

    def normalize(self):
        return None
""".lstrip(),
        encoding="utf-8",
    )
    (repository / "src" / "namespace" / "utility.py").write_text(
        "def utility():\n    return 1\n",
        encoding="utf-8",
    )
    (repository / "tests" / "test_service.py").write_text(
        """
from sample.service import Worker


def test_worker():
    worker = Worker()
    assert worker is not None
""".lstrip(),
        encoding="utf-8",
    )
    (repository / "vendor" / "vendored.py").write_text(
        "def excluded_vendor():\n    pass\n",
        encoding="utf-8",
    )
    (repository / "ignored.py").write_text(
        "def excluded_ignored():\n    pass\n",
        encoding="utf-8",
    )
    (repository / "generated_pb2.py").write_text(
        "def excluded_generated():\n    pass\n",
        encoding="utf-8",
    )
    (repository / "notebook.ipynb").write_text("{}\n", encoding="utf-8")
    run_git(repository, "add", ".")
    run_git(repository, "commit", "-m", "Add Python project")

    (repository / "tests" / "test_generated.py").write_text(
        "from sample.service import helper\n\n\ndef test_helper():\n    assert helper(1)\n",
        encoding="utf-8",
    )
    return repository


def test_builds_verified_graph_and_okf(python_repository: Path) -> None:
    result = build_knowledge(str(python_repository))

    assert result.action == "built"
    assert result.validation_status == "valid"
    assert result.file_count == 6
    graph_path = result.knowledge_root / "repo_graph.json"
    graph_text = graph_path.read_text(encoding="utf-8")
    graph = json.loads(graph_text)

    assert str(python_repository) not in graph_text
    assert graph["repository"]["source_roots"] == ["src"]
    assert graph["repository"]["test_roots"] == ["tests"]
    node_ids = {node["id"] for node in graph["nodes"]}
    assert "file:src/sample/service.py" in node_ids
    assert "symbol:sample.service:Worker" in node_ids
    assert "symbol:sample.service:Worker.run" in node_ids
    assert "symbol:sample.service:Worker.run.nested" in node_ids
    assert "symbol:namespace.utility:utility" in node_ids
    assert not any("vendored" in node_id for node_id in node_ids)
    assert not any("generated_pb2" in node_id for node_id in node_ids)
    edge_kinds = {edge["kind"] for edge in graph["edges"]}
    assert {
        "contains",
        "imports",
        "calls",
        "inherits",
        "decorated_by",
        "tests",
    } <= edge_kinds
    assert all(
        edge["source"] in node_ids and edge["target"] in node_ids
        for edge in graph["edges"]
    )
    assert all(edge["source_text_hash"] for edge in graph["edges"])

    validation = json.loads(
        (result.knowledge_root / "validation.json").read_text(encoding="utf-8")
    )
    assert validation["graph"]["status"] == "valid"
    assert validation["okf"]["status"] == "valid"
    assert (result.knowledge_root / ".okf" / "index.md").is_file()
    assert (
        result.knowledge_root / ".okf" / "modules" / "src" / "sample" / "service.py.md"
    ).is_file()
    assert (
        result.knowledge_root / ".okf" / "tests" / "tests" / "test_service.py.md"
    ).is_file()
    assert "inverse-alpha/0.2.0" in (
        result.knowledge_root / ".okf" / "repository.md"
    ).read_text(encoding="utf-8")

    status = run_git(
        python_repository, "status", "--porcelain", "--untracked-files=all"
    )
    assert ".inverse_alpha" not in status


def test_repeated_run_reuses_byte_identical_artifacts(python_repository: Path) -> None:
    first = build_knowledge(str(python_repository))
    artifact_paths = [
        first.knowledge_root / "repo_graph.json",
        first.knowledge_root / "diagnostics.jsonl",
        first.knowledge_root / "annotations.jsonl",
        first.knowledge_root / "validation.json",
        first.knowledge_root / "state.json",
        *sorted((first.knowledge_root / ".okf").rglob("*.md")),
    ]
    before = {
        path.relative_to(first.knowledge_root): path.read_bytes()
        for path in artifact_paths
    }

    second = build_knowledge(str(python_repository))
    after = {
        path.relative_to(second.knowledge_root): path.read_bytes()
        for path in artifact_paths
    }

    assert second.action == "reused"
    assert after == before


def test_changed_file_reuses_unchanged_parse_cache(python_repository: Path) -> None:
    first = build_knowledge(str(python_repository))
    original_digest = first.source_digest
    service_path = python_repository / "src" / "sample" / "service.py"
    service_path.write_text(
        service_path.read_text(encoding="utf-8")
        + "\n\ndef added():\n    return helper(2)\n",
        encoding="utf-8",
    )

    second = build_knowledge(str(python_repository))

    assert second.action == "built"
    assert second.source_digest != original_digest
    graph = json.loads(
        (second.knowledge_root / "repo_graph.json").read_text(encoding="utf-8")
    )
    assert any(node["id"] == "symbol:sample.service:added" for node in graph["nodes"])
    log_records = [
        json.loads(line)
        for line in (python_repository / ".inverse_alpha" / "logs" / "knowledge.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert log_records[-1]["counts"]["cache_hits"] == 5
    assert log_records[-1]["counts"]["cache_misses"] == 1


def test_syntax_error_fails_and_records_diagnostic(tmp_path: Path) -> None:
    repository = tmp_path / "broken-python"
    repository.mkdir()
    run_git(repository, "init", "-b", "main")
    run_git(repository, "config", "user.name", "Inverse Alpha Test")
    run_git(repository, "config", "user.email", "inverse-alpha@example.test")
    (repository / "broken.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
    run_git(repository, "add", "broken.py")
    run_git(repository, "commit", "-m", "Add broken source")

    with pytest.raises(InputError, match="could not parse"):
        build_knowledge(str(repository))

    diagnostics = (
        repository / ".inverse_alpha" / "knowledge" / "diagnostics.jsonl"
    ).read_text(encoding="utf-8")
    assert "tree_sitter_parse_error" in diagnostics
    manifest = json.loads(
        (repository / ".inverse_alpha" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["stages"]["knowledge_layer"] == "partial"


def test_no_python_source_fails(history_repository: Path) -> None:
    with pytest.raises(InputError, match="No Python source files"):
        build_knowledge(str(history_repository))


def test_knowledge_cli_prints_summary(
    python_repository: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["knowledge", str(python_repository)]) == 0
    output = capsys.readouterr().out
    assert "Source digest:" in output
    assert "Unresolved references:" in output
    assert "Validation: valid" in output


def test_flat_layout_multiline_conditional_imports_and_unittest(tmp_path: Path) -> None:
    repository = tmp_path / "flat-python"
    repository.mkdir()
    run_git(repository, "init", "-b", "main")
    run_git(repository, "config", "user.name", "Inverse Alpha Test")
    run_git(repository, "config", "user.email", "inverse-alpha@example.test")
    (repository / "models.py").write_text(
        "class Alpha:\n    pass\n\nclass Beta:\n    pass\n",
        encoding="utf-8",
    )
    (repository / "app.pyi").write_text(
        "def typed(value: int) -> int: ...\n", encoding="utf-8"
    )
    (repository / "app.py").write_text(
        """
from typing import TYPE_CHECKING
from models import (
    Alpha,
    Beta as RenamedBeta,
)

if TYPE_CHECKING:
    from app import typed


def create():
    return Alpha(), RenamedBeta()
""".lstrip(),
        encoding="utf-8",
    )
    (repository / "test_app.py").write_text(
        """
import unittest
from app import create


class AppTest(unittest.TestCase):
    def test_create(self):
        self.assertEqual(len(create()), 2)
""".lstrip(),
        encoding="utf-8",
    )
    run_git(repository, "add", ".")
    run_git(repository, "commit", "-m", "Add flat project")

    result = build_knowledge(str(repository))
    graph = json.loads(
        (result.knowledge_root / "repo_graph.json").read_text(encoding="utf-8")
    )

    assert graph["repository"]["source_roots"] == ["."]
    assert {"file:app.py", "file:app.pyi", "file:models.py", "file:test_app.py"} <= {
        node["id"] for node in graph["nodes"]
    }
    import_edges = [edge for edge in graph["edges"] if edge["kind"] == "imports"]
    assert any(edge["target"] == "symbol:models:Alpha" for edge in import_edges)
    assert any(edge["target"] == "symbol:models:Beta" for edge in import_edges)
    assert graph["statistics"]["tests"] > 0


def test_deleted_and_renamed_files_leave_no_stale_nodes(
    python_repository: Path,
) -> None:
    first = build_knowledge(str(python_repository))
    assert first.validation_status == "valid"
    run_git(
        python_repository,
        "mv",
        "src/namespace/utility.py",
        "src/namespace/tools.py",
    )
    (python_repository / "src" / "sample" / "base.py").unlink()
    (python_repository / "src" / "sample" / "service.py").write_text(
        (python_repository / "src" / "sample" / "service.py")
        .read_text(encoding="utf-8")
        .replace("from .base import Base\n", "")
        .replace("class Worker(Base):", "class Worker:"),
        encoding="utf-8",
    )

    second = build_knowledge(str(python_repository))
    graph = json.loads(
        (second.knowledge_root / "repo_graph.json").read_text(encoding="utf-8")
    )
    node_ids = {node["id"] for node in graph["nodes"]}

    assert "file:src/namespace/utility.py" not in node_ids
    assert "symbol:namespace.utility:utility" not in node_ids
    assert "file:src/sample/base.py" not in node_ids
    assert "file:src/namespace/tools.py" in node_ids
    assert "symbol:namespace.tools:utility" in node_ids
