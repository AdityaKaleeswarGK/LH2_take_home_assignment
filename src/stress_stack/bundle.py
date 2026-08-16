"""Assemble the deliverable in the layout the brief asks for.

The pipeline writes its working state into the analysed repository, under
``.stress_stack/``, because that keeps every artifact beside the code it
describes and lets any stage be re-run independently. The brief asks for a
different shape — an ``output/`` tree holding the transformed repository, its
graph, an ``.okf/`` knowledge directory, ``tasks/`` and ``tasks.json`` at the
root, and ``transcripts/`` — so this projects one onto the other.

Nothing is recomputed here and nothing is decided here. If the pipeline
produced a shortfall, the shortfall is copied out with everything else.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stress_stack.atomic import atomic_write_json, atomic_write_text

# Files the transformed repository is expected to carry out with it: the
# pinned lock, the container definition, and the adopted lint baseline.
_TRANSFORMED_MARKERS = ("requirements.lock", "Dockerfile", ".dockerignore", "ruff.toml")


@dataclass
class BundleResult:
    output_root: Path
    repository_root: Path
    task_count: int = 0
    transcripts: int = 0
    copied: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_root": str(self.output_root),
            "repository_root": str(self.repository_root),
            "task_count": self.task_count,
            "transcripts": self.transcripts,
            "copied": self.copied,
            "missing": self.missing,
        }


def assemble(repository_root: Path, output_root: Path) -> BundleResult:
    """Project ``.stress_stack/`` into the brief's ``output/`` layout."""
    metadata = repository_root / ".stress_stack"
    result = BundleResult(output_root=output_root, repository_root=repository_root)
    if output_root.exists():
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    # 1. The transformed repository: pinned, containerised, tested, lint-clean.
    repo_out = output_root / "repo"
    shutil.copytree(
        repository_root,
        repo_out,
        ignore=shutil.ignore_patterns(
            ".git", ".stress_stack", "__pycache__", "*.pyc", ".pytest_cache"
        ),
    )
    result.copied.append("repo/")
    for marker in _TRANSFORMED_MARKERS:
        if not (repo_out / marker).is_file():
            result.missing.append(marker)

    # 2. The knowledge layer, under the name the brief uses.
    knowledge = metadata / "knowledge"
    okf = output_root / ".okf"
    if knowledge.is_dir():
        shutil.copytree(knowledge, okf)
        result.copied.append(".okf/")
        graph = okf / "repo_graph.json"
        if graph.is_file():
            shutil.copy2(graph, output_root / "repo_graph.json")
            result.copied.append("repo_graph.json")
    else:
        result.missing.append("knowledge/")

    # 3. Tasks and the manifest, at the root rather than nested.
    tasks = metadata / "tasks"
    if tasks.is_dir():
        shipped = _shipped_ids(metadata / "tasks.json")
        destination = output_root / "tasks"
        destination.mkdir(parents=True, exist_ok=True)
        for entry in sorted(tasks.iterdir()):
            if not entry.is_dir():
                continue
            # Validation keeps every attempted tree as evidence; the deliverable
            # carries the ten that ship. The rest stay in the repository for
            # anyone re-deriving the funnel.
            if shipped and entry.name not in shipped:
                continue
            shutil.copytree(entry, destination / entry.name)
            result.task_count += 1
        result.copied.append(f"tasks/ ({result.task_count})")
    else:
        result.missing.append("tasks/")

    manifest = metadata / "tasks.json"
    if manifest.is_file():
        shutil.copy2(manifest, output_root / "tasks.json")
        result.copied.append("tasks.json")
    else:
        result.missing.append("tasks.json")

    result.transcripts = _write_transcripts(metadata, output_root / "transcripts")
    if result.transcripts:
        result.copied.append(f"transcripts/ ({result.transcripts})")

    atomic_write_json(output_root / "bundle.json", result.to_dict())
    return result


def _shipped_ids(manifest_path: Path) -> set[str]:
    if not manifest_path.is_file():
        return set()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return {str(task.get("id")) for task in payload.get("tasks") or [] if task.get("id")}


def _write_transcripts(metadata: Path, destination: Path) -> int:
    """Every model exchange, as the request and response actually sent.

    The cache is already a complete transcript — each entry holds the full
    request payload and the response — so this is a rename rather than a
    reconstruction. The key never appears in it: entries are written through
    ``config.redact``.
    """
    cache = metadata / "cache" / "llm"
    if not cache.is_dir():
        return 0
    destination.mkdir(parents=True, exist_ok=True)
    written = 0
    index: list[dict[str, Any]] = []
    for entry in sorted(cache.glob("*.json")):
        try:
            payload = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        shutil.copy2(entry, destination / entry.name)
        written += 1
        request = payload.get("request") or {}
        index.append(
            {
                "cache_key": payload.get("cache_key"),
                "model": payload.get("model"),
                "prompt_version": payload.get("prompt_version"),
                "system": _first_role(request, "system")[:200],
                "user_preview": _first_role(request, "user")[:200],
            }
        )
    atomic_write_text(
        destination / "README.md",
        "# Transcripts\n\n"
        f"{written} model exchanges, one JSON file per call, keyed by "
        "`sha256(prompt_version + request payload)`.\n\n"
        "Each file holds the exact request sent and the response received, which "
        "is what makes a re-run reproduce byte-identically: the cache is replayed "
        "rather than the model re-queried. Temperature zero does not guarantee "
        "that; a content-addressed cache does.\n\n"
        "API keys never appear here — entries are written through `config.redact`.\n",
    )
    atomic_write_json(destination / "index.json", {"calls": index})
    return written


def _first_role(request: dict[str, Any], role: str) -> str:
    for message in request.get("messages") or []:
        if message.get("role") == role:
            return str(message.get("content") or "")
    return ""
