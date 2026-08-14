from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from inverse_alpha.errors import MetadataError


def _json_line(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise MetadataError(f"Could not write metadata file {path}: {exc}") from exc


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    content = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    atomic_write_text(path, content)


def atomic_write_jsonl(path: Path, values: Iterable[Mapping[str, Any]]) -> None:
    content = "".join(f"{_json_line(value)}\n" for value in values)
    atomic_write_text(path, content)


def append_jsonl(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = f"{_json_line(value)}\n".encode("utf-8")
    try:
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, line)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise MetadataError(f"Could not append metadata log {path}: {exc}") from exc


def atomic_replace_directory(path: Path, files: Mapping[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{path.name}.", dir=path.parent))
    backup = path.parent / f".{path.name}.previous"
    try:
        for relative_path, content in sorted(files.items()):
            destination = temporary / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
        if backup.exists():
            shutil.rmtree(backup)
        if path.exists():
            os.replace(path, backup)
        os.replace(temporary, path)
        if backup.exists():
            shutil.rmtree(backup)
    except OSError as exc:
        if not path.exists() and backup.exists():
            os.replace(backup, path)
        raise MetadataError(f"Could not replace metadata directory {path}: {exc}") from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
