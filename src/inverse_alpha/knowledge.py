from __future__ import annotations

import hashlib
import json
import time
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from inverse_alpha.atomic import (
    append_jsonl,
    atomic_replace_directory,
    atomic_write_json,
    atomic_write_jsonl,
)
from inverse_alpha.errors import InputError, MetadataError
from inverse_alpha.git_repository import GitRepository
from inverse_alpha.graph_builder import build_repository_graph
from inverse_alpha.ingest import ingest
from inverse_alpha.knowledge_models import (
    KnowledgeEnricher,
    NullKnowledgeEnricher,
    require_valid_graph,
)
from inverse_alpha.models import AvailabilityEntry
from inverse_alpha.okf import render_okf_bundle, validate_okf_bundle
from inverse_alpha.python_parser import (
    EXTRACTOR_NAME,
    EXTRACTOR_VERSION,
    ParsedFile,
    PythonSourceParser,
)

_EXCLUDED_PARTS = {
    ".inverse_alpha",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "generated",
    "node_modules",
    "site-packages",
    "vendor",
    "vendored",
    "vendors",
    "venv",
}
_GENERATED_SUFFIXES = ("_pb2.py", "_pb2_grpc.py", "_generated.py")


@dataclass(frozen=True, slots=True)
class SourceFile:
    path: str
    content: bytes
    content_hash: str
    module: str
    is_test: bool


@dataclass(frozen=True, slots=True)
class KnowledgeResult:
    repository_root: Path
    knowledge_root: Path
    head_sha: str
    source_digest: str
    file_count: int
    symbol_count: int
    edge_count: int
    unresolved_count: int
    validation_status: str
    action: str


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def build_knowledge(
    source_value: str,
    *,
    cwd: Path | None = None,
    enricher: KnowledgeEnricher | None = None,
) -> KnowledgeResult:
    started_at = utc_now()
    start_clock = time.monotonic()
    ingestion = ingest(source_value, cwd=cwd)
    repository = GitRepository.discover(ingestion.repository_root)
    repository.add_metadata_exclude()
    metadata_root = ingestion.metadata_root
    knowledge_root = metadata_root / "knowledge"
    log_path = metadata_root / "logs" / "knowledge.jsonl"

    try:
        relative_paths = discover_python_paths(repository)
        if not relative_paths:
            raise InputError(
                "No Python source files were found in the Git-aware working tree"
            )
        source_roots = detect_source_roots(repository.root, relative_paths)
        test_roots = detect_test_roots(relative_paths)
        sources = read_sources(repository.root, relative_paths, source_roots)
        source_digest = aggregate_source_digest(sources)
        dirty, _ = repository.dirty_status()
        state_path = knowledge_root / "state.json"
        previous_state = _read_json(state_path)

        if _can_reuse(
            previous_state, ingestion.head_sha, source_digest, knowledge_root
        ):
            graph_data = _read_json(knowledge_root / "repo_graph.json")
            validation = _read_json(knowledge_root / "validation.json")
            if graph_data is not None and validation is not None:
                result = _result_from_graph(
                    repository.root,
                    knowledge_root,
                    ingestion.head_sha,
                    source_digest,
                    graph_data,
                    validation,
                    "reused",
                )
                _append_log(
                    log_path,
                    started_at,
                    start_clock,
                    repository,
                    result,
                    cache_hits=len(sources),
                    cache_misses=0,
                    warnings=[],
                )
                return result

        parsed_files, diagnostics, cache_hits, cache_misses = _parse_sources(
            metadata_root, sources
        )
        parse_failures = [item for item in diagnostics if item["severity"] == "error"]
        if parse_failures:
            knowledge_root.mkdir(parents=True, exist_ok=True)
            atomic_write_jsonl(knowledge_root / "diagnostics.jsonl", diagnostics)
            _update_pipeline_metadata(
                metadata_root,
                status="partial",
                counts={"python_files": len(sources)},
                reasons={"symbol_extraction": "python_parse_failed"},
            )
            raise InputError(parse_failures[0]["message"])

        source_contents = {item.path: item.content for item in sources}
        graph = build_repository_graph(
            parsed_files,
            source_contents,
            repository_name=_repository_name(metadata_root, repository.root.name),
            head_sha=ingestion.head_sha,
            source_digest=source_digest,
            dirty=dirty,
            source_roots=source_roots,
            test_roots=test_roots,
        )
        graph_data = graph.to_dict()
        graph_validation = require_valid_graph(graph_data, source_contents)

        verified_at = utc_now()
        if previous_state and previous_state.get("source_digest") == source_digest:
            previous_verified_at = previous_state.get("verified_at")
            if isinstance(previous_verified_at, str):
                verified_at = previous_verified_at
        okf_files = render_okf_bundle(graph, verified_at, diagnostics)
        okf_validation = validate_okf_bundle(okf_files, graph, set(source_contents))
        if okf_validation["status"] != "valid":
            raise MetadataError(
                "Generated OKF bundle failed validation: "
                + "; ".join(okf_validation["errors"])
            )

        validation = {
            "schema_version": "0.2.0",
            "status": "valid",
            "graph": graph_validation,
            "okf": okf_validation,
        }
        annotations = (enricher or NullKnowledgeEnricher()).enrich(graph)
        state = {
            "schema_version": "0.2.0",
            "status": "complete",
            "head_sha": ingestion.head_sha,
            "source_digest": source_digest,
            "verified_at": verified_at,
            "extractor": {"name": EXTRACTOR_NAME, "version": EXTRACTOR_VERSION},
            "files": [
                {"path": item.path, "content_hash": item.content_hash}
                for item in sorted(sources, key=lambda value: value.path)
            ],
        }

        knowledge_root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(knowledge_root / "repo_graph.json", graph_data)
        atomic_write_jsonl(knowledge_root / "diagnostics.jsonl", diagnostics)
        atomic_write_jsonl(
            knowledge_root / "annotations.jsonl",
            (annotation.to_dict() for annotation in annotations),
        )
        atomic_write_json(knowledge_root / "validation.json", validation)
        atomic_write_json(knowledge_root / "state.json", state)
        atomic_replace_directory(knowledge_root / ".okf", okf_files)
        _update_pipeline_metadata(
            metadata_root,
            status="complete",
            counts={
                "python_files": graph.statistics["files"],
                "symbols": graph.statistics["symbols"],
                "imports": graph.statistics["imports"],
                "calls": graph.statistics["calls"],
                "inheritance": graph.statistics["inherits"],
                "tests": graph.statistics["tests"],
                "okf_documents": okf_validation["document_count"],
            },
            reasons={},
        )
        result = KnowledgeResult(
            repository.root,
            knowledge_root,
            ingestion.head_sha,
            source_digest,
            graph.statistics["files"],
            graph.statistics["symbols"],
            graph.statistics["edges"],
            graph.statistics["unresolved_references"],
            validation["status"],
            "built",
        )
        _append_log(
            log_path,
            started_at,
            start_clock,
            repository,
            result,
            cache_hits,
            cache_misses,
            [item["message"] for item in diagnostics],
        )
        return result
    except Exception as exc:
        append_jsonl(
            log_path,
            {
                "event": "knowledge",
                "status": "failed",
                "started_at": started_at,
                "completed_at": utc_now(),
                "duration_seconds": round(time.monotonic() - start_clock, 3),
                "repository_root": str(repository.root),
                "head_sha": ingestion.head_sha,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "commands": repository.command_log,
            },
        )
        raise


def discover_python_paths(repository: GitRepository) -> list[str]:
    output = repository.run_bytes(["ls-files", "-co", "--exclude-standard", "-z"])
    values: list[str] = []
    for raw_path in output.split(b"\0"):
        if not raw_path:
            continue
        value = raw_path.decode("utf-8", errors="strict")
        path = PurePosixPath(value)
        lower_parts = {part.lower() for part in path.parts}
        if path.suffix not in {".py", ".pyi"}:
            continue
        if lower_parts & _EXCLUDED_PARTS:
            continue
        if path.name.endswith(_GENERATED_SUFFIXES) and not is_test_path(value):
            continue
        absolute = repository.root.joinpath(*path.parts)
        if absolute.is_file() and not absolute.is_symlink():
            values.append(path.as_posix())
    return sorted(set(values))


def detect_source_roots(repository_root: Path, paths: list[str]) -> list[str]:
    candidates: list[str] = []
    pyproject_path = repository_root / "pyproject.toml"
    if pyproject_path.exists():
        try:
            configuration = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
            tool = configuration.get("tool", {})
            setuptools = tool.get("setuptools", {})
            find = setuptools.get("packages", {}).get("find", {})
            where = find.get("where", [])
            if isinstance(where, str):
                where = [where]
            candidates.extend(item for item in where if isinstance(item, str))
            package_dir = setuptools.get("package-dir", {})
            if isinstance(package_dir, dict):
                candidates.extend(
                    item for item in package_dir.values() if isinstance(item, str)
                )
            poetry_packages = tool.get("poetry", {}).get("packages", [])
            if isinstance(poetry_packages, list):
                candidates.extend(
                    item["from"]
                    for item in poetry_packages
                    if isinstance(item, dict) and isinstance(item.get("from"), str)
                )
            hatch_packages = (
                tool.get("hatch", {})
                .get("build", {})
                .get("targets", {})
                .get("wheel", {})
                .get("packages", [])
            )
            if isinstance(hatch_packages, list):
                candidates.extend(
                    str(PurePosixPath(item).parent)
                    for item in hatch_packages
                    if isinstance(item, str)
                )
        except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError):
            pass
    usable = [
        root
        for root in (_normalize_root(item) for item in candidates)
        if _root_contains_python(root, paths)
    ]
    if usable:
        return sorted(set(usable))
    if _root_contains_python("src", paths) and (repository_root / "src").is_dir():
        return ["src"]
    return ["."]


def detect_test_roots(paths: list[str]) -> list[str]:
    roots: set[str] = set()
    for value in paths:
        path = PurePosixPath(value)
        for index, part in enumerate(path.parts[:-1]):
            if part.lower() in {"test", "tests"}:
                roots.add(PurePosixPath(*path.parts[: index + 1]).as_posix())
                break
    return sorted(roots)


def read_sources(
    root: Path, relative_paths: list[str], source_roots: list[str]
) -> list[SourceFile]:
    values = []
    for value in relative_paths:
        content = root.joinpath(*PurePosixPath(value).parts).read_bytes()
        values.append(
            SourceFile(
                path=value,
                content=content,
                content_hash=hashlib.sha256(content).hexdigest(),
                module=module_name(value, source_roots),
                is_test=is_test_path(value),
            )
        )
    return sorted(values, key=lambda item: item.path)


def aggregate_source_digest(sources: list[SourceFile]) -> str:
    digest = hashlib.sha256()
    for source in sorted(sources, key=lambda item: item.path):
        digest.update(source.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(source.content_hash.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def module_name(path_value: str, source_roots: list[str]) -> str:
    path = PurePosixPath(path_value)
    roots = sorted(
        source_roots, key=lambda value: len(PurePosixPath(value).parts), reverse=True
    )
    relative = path
    for root_value in roots:
        if root_value == ".":
            continue
        try:
            relative = path.relative_to(PurePosixPath(root_value))
            break
        except ValueError:
            continue
    parts = list(relative.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) or "__root__"


def is_test_path(path_value: str) -> bool:
    path = PurePosixPath(path_value)
    return (
        any(part.lower() in {"test", "tests"} for part in path.parts[:-1])
        or path.name.startswith("test_")
        or path.name.endswith("_test.py")
    )


def _parse_sources(
    metadata_root: Path, sources: list[SourceFile]
) -> tuple[list[ParsedFile], list[dict[str, Any]], int, int]:
    cache_root = metadata_root / "cache" / "knowledge" / "files"
    parser = PythonSourceParser()
    parsed: list[ParsedFile] = []
    diagnostics: list[dict[str, Any]] = []
    hits = 0
    misses = 0
    for source in sources:
        cache_key = hashlib.sha256(
            "\0".join(
                (
                    EXTRACTOR_VERSION,
                    source.path,
                    source.module,
                    source.content_hash,
                    str(source.is_test),
                )
            ).encode("utf-8")
        ).hexdigest()
        cache_path = cache_root / f"{cache_key}.json"
        cached = _read_json(cache_path)
        if cached and cached.get("extractor_version") == EXTRACTOR_VERSION:
            value = ParsedFile.from_dict(cached["parsed_file"])
            parsed.append(value)
            diagnostics.extend(value.diagnostics)
            hits += 1
            continue
        misses += 1
        try:
            value = parser.parse(
                source.path, source.module, source.content, source.is_test
            )
        except SyntaxError as exc:
            diagnostics.append(
                {
                    "path": source.path,
                    "severity": "error",
                    "code": "tree_sitter_parse_error",
                    "message": str(exc),
                }
            )
            continue
        parsed.append(value)
        diagnostics.extend(value.diagnostics)
        atomic_write_json(
            cache_path,
            {
                "schema_version": "0.2.0",
                "extractor_version": EXTRACTOR_VERSION,
                "parsed_file": value.to_dict(),
            },
        )
    return (
        sorted(parsed, key=lambda item: item.path),
        sorted(
            diagnostics,
            key=lambda item: (item.get("path", ""), item.get("line", 0), item["code"]),
        ),
        hits,
        misses,
    )


def _normalize_root(value: str) -> str:
    normalized = PurePosixPath(value).as_posix().strip("/")
    return normalized or "."


def _root_contains_python(root: str, paths: list[str]) -> bool:
    if root == ".":
        return bool(paths)
    prefix = root.rstrip("/") + "/"
    return any(path.startswith(prefix) for path in paths)


def _repository_name(metadata_root: Path, fallback: str) -> str:
    value = _read_json(metadata_root / "repository.json")
    if value:
        identity = value.get("identity")
        if isinstance(identity, dict) and isinstance(identity.get("repository"), str):
            return identity["repository"]
    return fallback


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _can_reuse(
    state: dict[str, Any] | None,
    head_sha: str,
    source_digest: str,
    knowledge_root: Path,
) -> bool:
    return bool(
        state
        and state.get("status") == "complete"
        and state.get("head_sha") == head_sha
        and state.get("source_digest") == source_digest
        and (knowledge_root / "repo_graph.json").is_file()
        and (knowledge_root / "validation.json").is_file()
        and (knowledge_root / ".okf" / "index.md").is_file()
    )


def _result_from_graph(
    root: Path,
    knowledge_root: Path,
    head_sha: str,
    source_digest: str,
    graph: dict[str, Any],
    validation: dict[str, Any],
    action: str,
) -> KnowledgeResult:
    statistics = graph["statistics"]
    return KnowledgeResult(
        root,
        knowledge_root,
        head_sha,
        source_digest,
        statistics["files"],
        statistics["symbols"],
        statistics["edges"],
        statistics["unresolved_references"],
        validation["status"],
        action,
    )


def _update_pipeline_metadata(
    metadata_root: Path,
    *,
    status: str,
    counts: dict[str, int],
    reasons: dict[str, str],
) -> None:
    manifest_path = metadata_root / "manifest.json"
    manifest = _read_json(manifest_path) or {}
    stages = manifest.setdefault("stages", {})
    if isinstance(stages, dict):
        stages["knowledge_layer"] = status
    manifest["updated_at"] = utc_now()
    atomic_write_json(manifest_path, manifest)

    availability_path = metadata_root / "availability.json"
    availability = _read_json(availability_path) or {
        "schema_version": "0.2.0",
        "generated_at": utc_now(),
        "capabilities": {},
    }
    capabilities = availability.setdefault("capabilities", {})
    capability_counts = {
        "python_discovery": counts.get("python_files"),
        "symbol_extraction": counts.get("symbols"),
        "python_imports": counts.get("imports"),
        "python_calls": counts.get("calls"),
        "python_inheritance": counts.get("inheritance"),
        "python_tests": counts.get("tests"),
        "okf": counts.get("okf_documents"),
    }
    for name, count in capability_counts.items():
        reason = reasons.get(name)
        entry_status = (
            "partial"
            if reason
            else ("available" if count is not None else "not_collected")
        )
        capabilities[name] = AvailabilityEntry(entry_status, count, reason).to_dict()
    capabilities["semantic_enrichment"] = AvailabilityEntry(
        "not_collected", None, "interface_only_no_provider"
    ).to_dict()
    availability["generated_at"] = utc_now()
    atomic_write_json(availability_path, availability)


def _append_log(
    path: Path,
    started_at: str,
    start_clock: float,
    repository: GitRepository,
    result: KnowledgeResult,
    cache_hits: int,
    cache_misses: int,
    warnings: list[str],
) -> None:
    append_jsonl(
        path,
        {
            "event": "knowledge",
            "status": "complete",
            "started_at": started_at,
            "completed_at": utc_now(),
            "duration_seconds": round(time.monotonic() - start_clock, 3),
            "repository_root": str(result.repository_root),
            "head_sha": result.head_sha,
            "source_digest": result.source_digest,
            "action": result.action,
            "commands": repository.command_log,
            "counts": {
                "files": result.file_count,
                "symbols": result.symbol_count,
                "edges": result.edge_count,
                "unresolved_references": result.unresolved_count,
                "cache_hits": cache_hits,
                "cache_misses": cache_misses,
            },
            "warnings": warnings,
        },
    )
