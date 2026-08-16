from __future__ import annotations

import json
from pathlib import Path

import pytest

from stress_stack.bundle import assemble
from stress_stack.errors import MetadataError


def repository(root: Path) -> None:
    (root / ".stress_stack" / "knowledge").mkdir(parents=True)
    (root / ".stress_stack" / "tasks" / "t1").mkdir(parents=True)
    (root / ".stress_stack" / "tasks.json").write_text(
        json.dumps({"tasks": [{"id": "t1"}]}), encoding="utf-8"
    )
    (root / "pkg.py").write_text("VALUE = 1\n", encoding="utf-8")
    for marker in ("requirements.lock", "Dockerfile", ".dockerignore", "ruff.toml"):
        (root / marker).write_text("generated\n", encoding="utf-8")


def test_nested_output_does_not_copy_itself_recursively(tmp_path: Path) -> None:
    repository(tmp_path)
    output = tmp_path / "output"

    result = assemble(tmp_path, output)

    assert result.output_root == output.resolve()
    assert (output / "repo" / "pkg.py").is_file()
    assert not (output / "repo" / "output").exists()
    assert (output / ".stress-stack-bundle.json").is_file()
    assert (output / "REPORT.md").is_file()
    assert (output / "transcripts" / "index.json").is_file()
    assert "tasks:expected_10_found_1" in result.missing


def test_refuses_source_or_unmanaged_nonempty_destination(tmp_path: Path) -> None:
    repository(tmp_path)
    with pytest.raises(MetadataError):
        assemble(tmp_path, tmp_path)

    destination = tmp_path.parent / f"{tmp_path.name}-user-data"
    destination.mkdir()
    (destination / "keep.txt").write_text("mine", encoding="utf-8")
    try:
        with pytest.raises(MetadataError):
            assemble(tmp_path, destination)
        assert (destination / "keep.txt").read_text() == "mine"
    finally:
        (destination / "keep.txt").unlink()
        destination.rmdir()
