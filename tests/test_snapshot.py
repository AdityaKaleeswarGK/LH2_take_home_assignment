from __future__ import annotations

from pathlib import Path

from stress_stack.snapshot import audit_solver_bundle, purge_bytecode


def write(root: Path, relative: str, content: str = "") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_purges_every_form_of_compiled_bytecode(tmp_path: Path) -> None:
    write(tmp_path, "pkg/__pycache__/mod.cpython-312.pyc", "x")
    write(tmp_path, "pkg/sub/__pycache__/other.pyc", "x")
    write(tmp_path, "stray.pyo", "x")
    write(tmp_path, "pkg/mod.py", "keep = 1\n")

    purge_bytecode(tmp_path)

    assert list(tmp_path.rglob("__pycache__")) == []
    assert list(tmp_path.rglob("*.pyc")) == []
    assert list(tmp_path.rglob("*.pyo")) == []
    assert (tmp_path / "pkg" / "mod.py").exists()


def test_audit_passes_on_a_clean_bundle(tmp_path: Path) -> None:
    write(tmp_path, "pkg/mod.py", "x = 1\n")
    write(tmp_path, ".gitignore", "*.pyc\n")

    report = audit_solver_bundle(tmp_path)

    assert report["clean"] is True
    assert report["leaked_paths"] == []


def test_audit_flags_history_solution_and_bytecode_leaks(tmp_path: Path) -> None:
    write(tmp_path, "pkg/mod.py", "x = 1\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / "solution").mkdir()
    write(tmp_path, "fix.patch", "diff --git a b\n")
    write(tmp_path, "pkg/__pycache__/mod.pyc", "x")

    report = audit_solver_bundle(tmp_path)

    assert report["clean"] is False
    assert report["leaked_paths"] == [".git", "solution"]
    assert report["stray_patches"] == ["fix.patch"]
    assert report["stale_bytecode"] == ["pkg/__pycache__"]
