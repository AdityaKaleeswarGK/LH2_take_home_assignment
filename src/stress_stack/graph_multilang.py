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

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from stress_stack.atomic import atomic_write_json
from stress_stack.symbols import Anchor
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


@dataclass(frozen=True, slots=True)
class MultiLangEdge:
    """The same shape ``graph.GraphEdge`` has, for the same consumers.

    Edges used to be 3-tuples here. That was enough for this module's own
    validation and nothing else: ``blast_radius`` reads ``edge.kind`` and
    ``edge.anchor``, so every non-Python task fell through to a scope listing
    only the files it had already been told about. A tuple cannot answer where
    the reference was written, which is the part a solver actually needs.
    """

    kind: str
    source: str
    target: str
    anchor: Anchor
    expression: str = ""

    def key(self) -> tuple[str, str, str, str, int, str]:
        return (
            self.kind,
            self.source,
            self.target,
            self.anchor.path,
            self.anchor.line,
            self.expression,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "kind": self.kind,
            "target": self.target,
            "anchor": self.anchor.to_dict(),
            "expression": self.expression,
        }


@dataclass
class MultiLanguageGraph:
    root: Path
    files: list[ParsedSourceFile] = field(default_factory=list)
    edges: list[MultiLangEdge] = field(default_factory=list)
    external_modules: list[str] = field(default_factory=list)

    def symbol_index(self) -> dict[str, Any]:
        """Mirrors ``RepositoryGraph.symbol_index`` so both graphs read alike."""
        return {
            symbol.id: symbol for parsed in self.files for symbol in parsed.symbols
        }

    def statistics(self) -> dict[str, int]:
        counts: dict[str, int] = {
            "files": len(self.files),
            "test_files": sum(1 for parsed in self.files if _is_test_path(parsed.path)),
            "symbols": sum(len(parsed.symbols) for parsed in self.files),
            "tests": sum(len(parsed.tests) for parsed in self.files),
            "edges": len(self.edges),
            "external_modules": len(self.external_modules),
            "syntax_errors": sum(1 for parsed in self.files if parsed.has_syntax_error),
            "parsed_by_fallback": sum(1 for parsed in self.files if parsed.parser == "regex"),
        }
        for kind in ("contains", "imports", "references"):
            counts[f"edges_{kind}"] = sum(1 for edge in self.edges if edge.kind == kind)
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
            "edges": [edge.to_dict() for edge in sorted(self.edges, key=MultiLangEdge.key)],
            "external_modules": sorted(self.external_modules),
        }


def _is_test_path(path: str) -> bool:
    """Whether a repository-relative path is a test file.

    The leading slash matters. Matching `/tests/` against a bare
    `tests/test_x.py` fails — the marker needs a boundary the string does not
    have at position zero — so a top-level test directory, which is the most
    common layout there is, read as source. It only looked correct because
    almost every file underneath one is also caught by its name.
    """
    lowered = "/" + path.lower().replace("\\", "/").lstrip("/")
    name = lowered.rsplit("/", 1)[-1]
    if name.startswith("test"):
        return True
    return any(marker in lowered for marker in _TEST_MARKERS)


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
            graph.edges.append(
                MultiLangEdge(
                    kind="contains",
                    source=relative,
                    target=symbol.id,
                    anchor=Anchor(
                        path=relative,
                        line=symbol.start_line,
                        end_line=symbol.end_line,
                        column=0,
                        end_column=0,
                    ),
                    expression=symbol.name,
                )
            )

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
            graph.edges.append(
                MultiLangEdge(
                    kind="imports",
                    source=parsed.path,
                    target=target or module,
                    anchor=Anchor(
                        path=parsed.path,
                        line=imported.line,
                        end_line=imported.line,
                        column=0,
                        end_column=0,
                    ),
                    expression=imported.raw.strip(),
                )
            )
            if target is None:
                external.add(module)

    graph.external_modules = sorted(external)
    _add_reference_edges(graph)
    return graph


# An identifier has to be at least this long before a name match counts as a
# reference. `id`, `at`, `ok` collide across unrelated files in every codebase;
# a three-character floor removes almost all of that without losing real names.
_MIN_REFERENCE_NAME = 3

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _add_reference_edges(graph: MultiLanguageGraph) -> None:
    """Which files mention a symbol another file defines.

    Without this the graph has only containment and imports, and `blast_radius`
    can answer "who references this" for exactly one case: a cross-file import.
    Go does not have that case within a package — `use.go` calls `Add` from
    `calc.go` with no import line at all — so every Go task shipped a scope
    listing only the file the solver was already looking at.

    This is a name match, not a resolved call, and it is deliberately not
    presented as one: the edge kind is `references`, and `blast_radius` caps how
    many it will report per file. A name match can be wrong in one direction
    only — it can name a file that turns out to be irrelevant, which costs a
    line of context. Missing the caller costs the solver the thing it needed.
    """
    owners: dict[str, set[str]] = {}
    for parsed in graph.files:
        for symbol in parsed.symbols:
            if len(symbol.name) < _MIN_REFERENCE_NAME or symbol.is_test:
                continue
            owners.setdefault(symbol.name, set()).add(parsed.path)

    for parsed in graph.files:
        try:
            code = (graph.root / parsed.path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        seen: set[tuple[str, str]] = set()
        for line_number, line in enumerate(code.splitlines(), start=1):
            for name in _IDENTIFIER.findall(line):
                if len(name) < _MIN_REFERENCE_NAME:
                    continue
                for owner_path in owners.get(name, ()):  # noqa: PLC0206
                    if owner_path == parsed.path:
                        continue
                    target = f"{owner_path}::{name}"
                    if (target, name) in seen:
                        continue
                    seen.add((target, name))
                    graph.edges.append(
                        MultiLangEdge(
                            kind="references",
                            source=parsed.path,
                            target=target,
                            anchor=Anchor(
                                path=parsed.path,
                                line=line_number,
                                end_line=line_number,
                                column=0,
                                end_column=0,
                            ),
                            expression=name,
                        )
                    )


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
    original = {edge.key() for edge in graph.edges}
    repeated = {edge.key() for edge in rebuilt.edges}
    matched = original & repeated
    total = len(original)
    valid_anchors, invalid_anchors = _anchors(graph, root)
    checked = valid_anchors + len(invalid_anchors)
    syntax_errors = sum(1 for parsed in graph.files if parsed.has_syntax_error)

    fallback_parsed = sorted(
        parsed.path for parsed in graph.files if parsed.parser == "regex"
    )

    edge_rate = round(len(matched) / total, 6) if total else 1.0
    anchor_rate = round(valid_anchors / checked, 6) if checked else 1.0
    # A graph nobody can reproduce is not a knowledge layer. Both rates must be
    # exact: a re-parse of unchanged source has no legitimate reason to differ.
    #
    # Two more conditions, both of which were computed and then ignored:
    #
    # * A file that did not parse describes nothing, and `syntax_errors` was
    #   already being counted and reported next to a `verified` status.
    # * A file parsed by the regex fallback cannot have its body extents
    #   trusted, which is the one thing excision depends on. Worse, the C/C++
    #   fallback used to emit `symbols=0, has_syntax_error=False` — a clean
    #   zero — so an entire unparsed C++ tree reported `verified` with nothing
    #   in it. Naming the parser is what makes that visible.
    problems = []
    if edge_rate != 1.0:
        problems.append("edges_did_not_reproduce")
    if anchor_rate != 1.0:
        problems.append("anchors_did_not_reproduce")
    if syntax_errors:
        problems.append(f"syntax_errors_in_{syntax_errors}_files")
    if fallback_parsed:
        problems.append(f"regex_fallback_parsed_{len(fallback_parsed)}_files")
    status = "verified" if not problems else "mismatched"

    return {
        "schema_version": "0.1.0",
        "builder": "tree_sitter",
        "status": status,
        "problems": problems,
        "edges_total": total,
        "edges_matched": len(matched),
        "edges_only_in_graph": [str(key) for key in sorted(original - repeated)[:20]],
        "edges_only_in_rebuild": [str(key) for key in sorted(repeated - original)[:20]],
        "edge_match_rate": edge_rate,
        "anchors_checked": checked,
        "anchors_valid": valid_anchors,
        "anchors_invalid": invalid_anchors[:20],
        "anchor_match_rate": anchor_rate,
        "files_with_syntax_errors": syntax_errors,
        "files_parsed_by_fallback": len(fallback_parsed),
        "fallback_parsed": fallback_parsed[:20],
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
    """Per-test coverage for a non-Python repository, written where mine reads it.

    Prefers the resolved workflow's attribution over the built-in one. The
    record only exists because its probe attributed a symbol in this very
    repository, so it is the stronger answer wherever the two differ — and for
    an ecosystem the built-in table has never heard of it is the only one.
    """
    from stress_stack.coverage_multilang import measure
    from stress_stack.workflow import COVERAGE, load_workflow

    root = Path(repo_root)
    knowledge_root = root / ".stress_stack" / "knowledge"
    knowledge_root.mkdir(parents=True, exist_ok=True)

    workflow = load_workflow(root / ".stress_stack" / "workflow.json")
    record = workflow.get(COVERAGE) if workflow else None

    graph = build_graph(root)
    coverage = measure(root, language, graph, workflow_record=record)
    atomic_write_json(knowledge_root / "coverage_map.json", coverage.to_dict())
    return {
        "repository_root": str(root),
        # The pipeline's semantic gate reads this key, so the vocabulary has to
        # match the Python stage's exactly.
        "coverage": coverage.status,
        "symbols": len(coverage.symbols),
        "covered_symbols": sum(1 for s in coverage.symbols.values() if s.covering_tests),
        "excisable_symbols": sum(
            1 for s in coverage.symbols.values() if s.covering_tests and not s.is_test
        ),
        "reason": coverage.reason or "",
        "attribution_source": record.source if record else "built_in",
        "knowledge_root": str(knowledge_root),
    }


def build_mining_artifacts(repo_root: Path | str, language: str) -> dict[str, Any]:
    """Rank candidates for a non-Python repository.

    Delegates. This used to be a second miner that produced excision candidates
    and declared history mining unimplemented — which meant a Go or Rust
    repository could never satisfy ``minimum: {history: 4}`` and could never
    ship ten tasks, however well every other stage worked.

    ``mine_history`` now reads source and test files through the profile rather
    than through ``.py``, so there is one miner and this is the ecosystem-aware
    entry point into it.
    """
    from stress_stack.graph import build_mining_artifacts as build
    from stress_stack.project_detector import detect_project_profile

    root = Path(repo_root)
    profile = detect_project_profile(root)
    if getattr(profile, "primary_language", "python") != language:
        # The caller has already detected the ecosystem; trust it over a second
        # detection, which can disagree on a tree hygiene has since rewritten.
        profile = replace(profile, primary_language=language)
    return build(str(root), cwd=root.parent, profile=profile)
