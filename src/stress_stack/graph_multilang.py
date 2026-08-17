"""A knowledge layer for repositories the Python graph cannot read.

Mirrors what ``graph.build_graph_artifacts`` produces — files, symbols, edges,
and a validation report — but sources its facts from tree-sitter instead of
``ast``. The artifact shape is deliberately the same so a consumer does not have
to know which builder ran.

The validation follows the same rule as the Python side: every edge is
re-derived from a second parse of the source and compared, and every symbol's
recorded line is checked to still contain that symbol's name. A graph that
cannot be reproduced from the source it claims to describe is reported as
mismatched rather than published as fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stress_stack.atomic import atomic_write_json
from stress_stack.parsers.tree_sitter_core import ParsedSourceFile, detect_language, parse_source_code

# Directories that contain code we did not write and must not describe as the
# repository's own. Vendored trees also dominate the symbol counts if included.
_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".stress_stack",
        ".venv",
        "venv",
        "node_modules",
        "vendor",
        "target",
        "build",
        "dist",
        "__pycache__",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        "third_party",
        "external",
    }
)

_TEST_MARKERS = ("test_", "_test.", ".test.", ".spec.", "/tests/", "/test/", "/spec/")


@dataclass
class MultiLanguageGraph:
    root: Path
    files: list[ParsedSourceFile] = field(default_factory=list)
    edges: list[tuple[str, str, str]] = field(default_factory=list)
    external_modules: list[str] = field(default_factory=list)

    def statistics(self) -> dict[str, int]:
        counts: dict[str, int] = {
            "files": len(self.files),
            "test_files": sum(1 for parsed in self.files if _is_test_path(parsed.path)),
            "symbols": sum(len(parsed.symbols) for parsed in self.files),
            "tests": sum(len(parsed.tests) for parsed in self.files),
            "edges": len(self.edges),
            "external_modules": len(self.external_modules),
            "syntax_errors": sum(1 for parsed in self.files if parsed.has_syntax_error),
        }
        for kind in ("contains", "imports"):
            counts[f"edges_{kind}"] = sum(1 for edge in self.edges if edge[1] == kind)
        by_language: dict[str, int] = {}
        for parsed in self.files:
            by_language[parsed.language] = by_language.get(parsed.language, 0) + 1
        for language, total in sorted(by_language.items()):
            counts[f"files_{language}"] = total
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "0.1.0",
            "builder": "tree_sitter",
            "statistics": self.statistics(),
            "files": [parsed.to_dict() for parsed in self.files],
            "edges": [
                {"source": source, "kind": kind, "target": target}
                for source, kind, target in self.edges
            ],
            "external_modules": sorted(self.external_modules),
        }


def _is_test_path(path: str) -> bool:
    lowered = path.lower().replace("\\", "/")
    name = lowered.rsplit("/", 1)[-1]
    return any(marker in lowered or name.startswith("test") for marker in _TEST_MARKERS)


def iter_source_files(root: Path) -> list[Path]:
    """Every parseable source file the repository itself owns."""
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        parts = path.relative_to(root).parts
        if any(part in _EXCLUDED_DIRECTORIES for part in parts):
            continue
        # Any hidden directory, not just the ones named above. `.claude/worktrees/`
        # holds a complete second copy of the repository, which doubled every
        # count and attributed the duplicate's symbols to the original.
        if any(part.startswith(".") for part in parts[:-1]):
            continue
        if detect_language(path) is None:
            continue
        found.append(path)
    return found


def build_graph(root: Path) -> MultiLanguageGraph:
    """Parse every source file and derive containment and import edges."""
    root = Path(root)
    graph = MultiLanguageGraph(root=root)

    for path in iter_source_files(root):
        relative = path.relative_to(root).as_posix()
        try:
            code = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        parsed = parse_source_code(relative, code)
        graph.files.append(parsed)

        for symbol in parsed.symbols:
            graph.edges.append((relative, "contains", f"{relative}::{symbol.name}"))

    # Import targets are resolved against the set of files actually present, so
    # "external" means "not in this repository" rather than "not on a list of
    # known standard library names" — a list that would need one entry per
    # ecosystem and would be wrong for every version skew.
    owned = {parsed.path for parsed in graph.files}
    owned_stems = {path.rsplit("/", 1)[-1].rsplit(".", 1)[0] for path in owned}
    external: set[str] = set()

    for parsed in graph.files:
        for imported in parsed.imports:
            module = imported.module.strip()
            if not module:
                continue
            target = _resolve_import(module, parsed.path, owned, owned_stems)
            graph.edges.append((parsed.path, "imports", target or module))
            if target is None:
                external.add(module)

    graph.external_modules = sorted(external)
    return graph


def _resolve_import(module: str, from_path: str, owned: set[str], owned_stems: set[str]) -> str | None:
    """Map an import to a file in this repository, or None when it is external."""
    candidate = module.strip("./").replace(".", "/").replace("::", "/")
    if not candidate:
        return None

    # A relative import resolves against the importing file's directory first.
    if module.startswith("."):
        base = from_path.rsplit("/", 1)[0] if "/" in from_path else ""
        joined = f"{base}/{module.lstrip('./')}" if base else module.lstrip("./")
        for owned_path in owned:
            stem = owned_path.rsplit(".", 1)[0]
            if stem == joined or stem.endswith(f"/{joined}"):
                return owned_path

    for owned_path in owned:
        stem = owned_path.rsplit(".", 1)[0]
        if stem == candidate or stem.endswith(f"/{candidate}"):
            return owned_path

    tail = candidate.rsplit("/", 1)[-1]
    if tail in owned_stems:
        for owned_path in owned:
            if owned_path.rsplit("/", 1)[-1].rsplit(".", 1)[0] == tail:
                return owned_path
    return None


def _anchors(graph: MultiLanguageGraph, root: Path) -> tuple[int, list[str]]:
    """Check each symbol's recorded line still contains its name."""
    valid = 0
    invalid: list[str] = []
    cache: dict[str, list[str]] = {}
    for parsed in graph.files:
        lines = cache.get(parsed.path)
        if lines is None:
            try:
                lines = (root / parsed.path).read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
            except OSError:
                continue
            cache[parsed.path] = lines
        for symbol in parsed.symbols:
            # Check the symbol's whole recorded span rather than a fixed window
            # after its first line. A definition preceded by attributes, macros
            # or a multi-line signature carries its name well past line+3, and
            # the anchor's claim is about the span, not about line one of it.
            span = lines[symbol.start_line - 1 : symbol.end_line]
            if any(symbol.name in line for line in span):
                valid += 1
            else:
                invalid.append(f"{parsed.path}::{symbol.name}@{symbol.start_line}")
    return valid, invalid


def validate_graph(graph: MultiLanguageGraph, root: Path) -> dict[str, Any]:
    """Re-derive the graph from source and report how much of it reproduces."""
    rebuilt = build_graph(root)
    original = {f"{s}|{k}|{t}" for s, k, t in graph.edges}
    repeated = {f"{s}|{k}|{t}" for s, k, t in rebuilt.edges}
    matched = original & repeated
    total = len(original)
    valid_anchors, invalid_anchors = _anchors(graph, root)
    checked = valid_anchors + len(invalid_anchors)
    syntax_errors = sum(1 for parsed in graph.files if parsed.has_syntax_error)

    edge_rate = round(len(matched) / total, 6) if total else 1.0
    anchor_rate = round(valid_anchors / checked, 6) if checked else 1.0
    # A graph nobody can reproduce is not a knowledge layer. Both rates must be
    # exact: a re-parse of unchanged source has no legitimate reason to differ.
    status = "verified" if edge_rate == 1.0 and anchor_rate == 1.0 else "mismatched"

    return {
        "schema_version": "0.1.0",
        "builder": "tree_sitter",
        "status": status,
        "edges_total": total,
        "edges_matched": len(matched),
        "edges_only_in_graph": sorted(original - repeated)[:20],
        "edges_only_in_rebuild": sorted(repeated - original)[:20],
        "edge_match_rate": edge_rate,
        "anchors_checked": checked,
        "anchors_valid": valid_anchors,
        "anchors_invalid": invalid_anchors[:20],
        "anchor_match_rate": anchor_rate,
        "files_with_syntax_errors": syntax_errors,
    }


@dataclass(frozen=True, slots=True)
class MultiLanguageGraphArtifacts:
    repository_root: Path
    knowledge_root: Path
    statistics: dict[str, int]
    validation: dict[str, Any]


def build_graph_artifacts(repo_root: Path | str) -> MultiLanguageGraphArtifacts:
    """Write the knowledge layer for a non-Python repository."""
    root = Path(repo_root)
    knowledge_root = root / ".stress_stack" / "knowledge"
    knowledge_root.mkdir(parents=True, exist_ok=True)

    graph = build_graph(root)
    validation = validate_graph(graph, root)

    atomic_write_json(knowledge_root / "repo_graph.json", graph.to_dict())
    atomic_write_json(knowledge_root / "graph_validation.json", validation)

    return MultiLanguageGraphArtifacts(
        repository_root=root,
        knowledge_root=knowledge_root,
        statistics=graph.statistics(),
        validation=validation,
    )


def build_coverage_artifacts(repo_root: Path | str, language: str) -> dict[str, Any]:
    """Per-test coverage for a non-Python repository, written where mine reads it."""
    from stress_stack.coverage_multilang import measure

    root = Path(repo_root)
    knowledge_root = root / ".stress_stack" / "knowledge"
    knowledge_root.mkdir(parents=True, exist_ok=True)

    graph = build_graph(root)
    coverage = measure(root, language, graph)
    atomic_write_json(knowledge_root / "coverage_map.json", coverage.to_dict())
    return {
        "repository_root": str(root),
        # The pipeline's semantic gate reads this key, so the vocabulary has to
        # match the Python stage's exactly.
        "coverage": coverage.status,
        "symbols": len(coverage.symbols),
        "covered_symbols": sum(1 for s in coverage.symbols.values() if s.covering_tests),
        "reason": coverage.reason or "",
        "knowledge_root": str(knowledge_root),
    }


def build_mining_artifacts(repo_root: Path | str, language: str) -> dict[str, Any]:
    """Rank excision candidates for a non-Python repository.

    History mining is deliberately absent. It reads pull requests and commit
    links that the ingest stage records for any repository, but ranking a
    history candidate needs the test-file classification and per-symbol
    attribution that only exists once mining is language-aware end to end;
    shipping a half-ranked history pool would put unvalidated tasks into the
    quota. Excision candidates are fully measured and are reported alone.
    """
    from stress_stack.candidates import mine_excision, module_distribution
    from stress_stack.coverage_map import load as load_coverage

    root = Path(repo_root)
    knowledge_root = root / ".stress_stack" / "knowledge"
    knowledge_root.mkdir(parents=True, exist_ok=True)

    graph = build_graph(root)
    coverage = load_coverage(knowledge_root / "coverage_map.json")
    excision, funnel = mine_excision(coverage, graph)

    payload = {
        "schema_version": "0.1.0",
        "coverage_status": coverage.status,
        "thresholds": {},
        "history": {
            "funnel": {"considered": 0, "kept": 0, "dropped_counts": {}, "dropped": {}},
            "module_distribution": {},
            "candidates": [],
            "note": f"history mining is not implemented for {language}",
        },
        "excision": {
            "funnel": funnel.to_dict(),
            "module_distribution": module_distribution(excision),
            "candidates": [candidate.to_dict() for candidate in excision],
        },
    }
    atomic_write_json(knowledge_root / "candidates.json", payload)
    return {
        "repository_root": str(root),
        "coverage_status": coverage.status,
        "history_count": 0,
        "excision_count": len(excision),
        "excision_funnel": funnel.to_dict(),
        "excision_modules": module_distribution(excision),
        "knowledge_root": str(knowledge_root),
    }
