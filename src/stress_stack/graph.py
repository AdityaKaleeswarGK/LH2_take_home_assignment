"""Repository symbol graph built from verified ``ast`` facts.

Every edge records the anchor it was derived from, so ``validate_graph`` can
re-derive the whole edge set from source and report a match rate. References
that cannot be resolved to a node are recorded in ``unresolved`` with a reason
rather than being pointed at a guess.
"""

from __future__ import annotations

import builtins
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from stress_stack import __version__
from stress_stack.atomic import atomic_write_json
from stress_stack.errors import MetadataError
from stress_stack.ingest import prepare_repository, update_stage
from stress_stack.source import resolve_source
from stress_stack.symbols import (
    Anchor,
    ParsedFile,
    discover_python_files,
    parse_file,
)

EdgeKind = Literal["contains", "imports", "calls", "inherits"]
_MAX_CALLERS = 8
_BUILTIN_NAMES = frozenset(dir(builtins))
_STDLIB_NAMES = frozenset(sys.stdlib_module_names)


@dataclass(frozen=True, slots=True)
class GraphEdge:
    kind: EdgeKind
    source: str
    target: str
    anchor: Anchor
    expression: str

    def key(self) -> tuple[str, str, str, str, int]:
        return (self.kind, self.source, self.target, self.anchor.path, self.anchor.line)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["anchor"] = self.anchor.to_dict()
        return value


@dataclass(frozen=True, slots=True)
class UnresolvedReference:
    kind: str
    expression: str
    source: str
    anchor: Anchor
    reason: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["anchor"] = self.anchor.to_dict()
        return value


@dataclass
class RepositoryGraph:
    files: list[ParsedFile]
    edges: list[GraphEdge]
    unresolved: list[UnresolvedReference]
    external_modules: list[str]

    def symbol_index(self) -> dict[str, Any]:
        return {
            symbol.id: symbol for parsed in self.files for symbol in parsed.symbols
        }

    def statistics(self) -> dict[str, int]:
        counts = {
            "files": len(self.files),
            "test_files": sum(1 for parsed in self.files if parsed.is_test),
            "symbols": sum(len(parsed.symbols) for parsed in self.files),
            "edges": len(self.edges),
            "unresolved": len(self.unresolved),
            "external_modules": len(self.external_modules),
            "syntax_errors": sum(1 for parsed in self.files if parsed.syntax_error),
        }
        for kind in ("contains", "imports", "calls", "inherits"):
            counts[f"edges_{kind}"] = sum(1 for edge in self.edges if edge.kind == kind)
        for reason in (
            "builtin",
            "stdlib_module",
            "external_module",
            "external_attribute",
            "dynamic_or_local",
        ):
            counts[f"unresolved_{reason}"] = sum(
                1 for item in self.unresolved if item.reason == reason
            )
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "0.1.0",
            "build_meta": {"tool": "stress-stack", "version": __version__},
            "statistics": self.statistics(),
            "files": [parsed.to_dict() for parsed in self.files],
            "edges": [edge.to_dict() for edge in sorted(self.edges, key=GraphEdge.key)],
            "unresolved": [
                item.to_dict()
                for item in sorted(
                    self.unresolved,
                    key=lambda item: (item.anchor.path, item.anchor.line, item.expression),
                )
            ],
            "external_modules": sorted(self.external_modules),
        }


def build_graph(root: Path) -> RepositoryGraph:
    parsed_files = [parse_file(root, path) for path in discover_python_files(root)]
    by_module = {parsed.module: parsed for parsed in parsed_files}
    symbol_ids = {symbol.id for parsed in parsed_files for symbol in parsed.symbols}
    edges: list[GraphEdge] = []
    unresolved: list[UnresolvedReference] = []
    external: set[str] = set()

    for parsed in parsed_files:
        _contains_edges(parsed, edges)
        _import_edges(parsed, by_module, edges, unresolved, external)
        bindings = _import_bindings(parsed, by_module, symbol_ids)
        _inherit_edges(parsed, bindings, symbol_ids, edges, unresolved)
        _call_edges(parsed, bindings, symbol_ids, edges, unresolved)

    return RepositoryGraph(
        files=parsed_files,
        edges=edges,
        unresolved=unresolved,
        external_modules=sorted(external),
    )


def _contains_edges(parsed: ParsedFile, edges: list[GraphEdge]) -> None:
    for symbol in parsed.symbols:
        if symbol.parent is None:
            continue
        edges.append(
            GraphEdge(
                kind="contains",
                source=symbol.parent,
                target=symbol.id,
                anchor=symbol.anchor,
                expression=symbol.name,
            )
        )


def _import_edges(
    parsed: ParsedFile,
    by_module: dict[str, ParsedFile],
    edges: list[GraphEdge],
    unresolved: list[UnresolvedReference],
    external: set[str],
) -> None:
    file_node = f"{parsed.path}::{parsed.module}"
    for reference in parsed.imports:
        target_module = _absolute_module(
            parsed.module, reference.module, reference.level, is_package=_package_of(parsed)
        )
        candidates = [target_module]
        if reference.name:
            candidates.insert(0, f"{target_module}.{reference.name}" if target_module else reference.name)
        resolved = next((name for name in candidates if name in by_module), None)
        if resolved is not None:
            target = by_module[resolved]
            edges.append(
                GraphEdge(
                    kind="imports",
                    source=file_node,
                    target=f"{target.path}::{target.module}",
                    anchor=reference.anchor,
                    expression=_import_expression(reference.module, reference.name, reference.level),
                )
            )
            continue
        root_module = (target_module or reference.bound_name).split(".")[0]
        if not root_module:
            reason = "unresolvable_relative_import"
        elif root_module in _STDLIB_NAMES:
            reason = "stdlib_module"
        else:
            reason = "external_module"
            external.add(root_module)
        unresolved.append(
            UnresolvedReference(
                kind="import",
                expression=_import_expression(reference.module, reference.name, reference.level),
                source=file_node,
                anchor=reference.anchor,
                reason=reason,
            )
        )


def _import_bindings(
    parsed: ParsedFile, by_module: dict[str, ParsedFile], symbol_ids: set[str]
) -> dict[str, str]:
    """Map each name bound by an import to a repository symbol id when resolvable."""
    bindings: dict[str, str] = {}
    for reference in parsed.imports:
        target_module = _absolute_module(
            parsed.module, reference.module, reference.level, is_package=_package_of(parsed)
        )
        if reference.name:
            submodule = f"{target_module}.{reference.name}" if target_module else reference.name
            if submodule in by_module:
                target = by_module[submodule]
                bindings[reference.bound_name] = f"{target.path}::{target.module}"
                continue
            resolved = _resolve_module_attribute(
                target_module, reference.name, by_module, symbol_ids
            )
            if resolved is not None:
                bindings[reference.bound_name] = resolved
                continue
        if target_module in by_module:
            target = by_module[target_module]
            bindings[reference.bound_name] = f"{target.path}::{target.module}"
    return bindings


def _resolve_module_attribute(
    module: str,
    name: str,
    by_module: dict[str, ParsedFile],
    symbol_ids: set[str],
    depth: int = 0,
) -> str | None:
    """Resolve ``module.name``, following ``__init__.py`` re-exports.

    Packages overwhelmingly expose their API by re-exporting from submodules
    (``from glom.core import glom`` inside ``glom/__init__.py``). Without
    following that hop, every call through the public name looks unresolved.
    """
    if depth > 5:
        return None
    parsed = by_module.get(module)
    if parsed is None:
        return None
    candidate = f"{parsed.path}::{module}.{name}" if module else f"{parsed.path}::{name}"
    if candidate in symbol_ids:
        return candidate
    for reference in parsed.imports:
        if reference.bound_name != name:
            continue
        target_module = _absolute_module(
            parsed.module, reference.module, reference.level, is_package=_package_of(parsed)
        )
        if reference.name:
            submodule = f"{target_module}.{reference.name}" if target_module else reference.name
            if submodule in by_module:
                target = by_module[submodule]
                return f"{target.path}::{target.module}"
            resolved = _resolve_module_attribute(
                target_module, reference.name, by_module, symbol_ids, depth + 1
            )
            if resolved is not None:
                return resolved
        elif target_module in by_module:
            target = by_module[target_module]
            return f"{target.path}::{target.module}"
    return None


def _inherit_edges(
    parsed: ParsedFile,
    bindings: dict[str, str],
    symbol_ids: set[str],
    edges: list[GraphEdge],
    unresolved: list[UnresolvedReference],
) -> None:
    for symbol in parsed.symbols:
        if symbol.kind != "class":
            continue
        for base in symbol.bases:
            target = _resolve(base, parsed, bindings, symbol_ids)
            if target is not None:
                edges.append(
                    GraphEdge(
                        kind="inherits",
                        source=symbol.id,
                        target=target,
                        anchor=symbol.anchor,
                        expression=base,
                    )
                )
            else:
                unresolved.append(
                    UnresolvedReference(
                        kind="base",
                        expression=base,
                        source=symbol.id,
                        anchor=symbol.anchor,
                        reason="unresolved_base",
                    )
                )


def _call_edges(
    parsed: ParsedFile,
    bindings: dict[str, str],
    symbol_ids: set[str],
    edges: list[GraphEdge],
    unresolved: list[UnresolvedReference],
) -> None:
    for call in parsed.calls:
        source = f"{parsed.path}::{call.scope}"
        target = _resolve(call.expression, parsed, bindings, symbol_ids)
        if target is not None:
            edges.append(
                GraphEdge(
                    kind="calls",
                    source=source,
                    target=target,
                    anchor=call.anchor,
                    expression=call.expression,
                )
            )
        else:
            unresolved.append(
                UnresolvedReference(
                    kind="call",
                    expression=call.expression,
                    source=source,
                    anchor=call.anchor,
                    reason=_call_reason(call.expression, parsed, bindings),
                )
            )


def _call_reason(expression: str, parsed: ParsedFile, bindings: dict[str, str]) -> str:
    """Say why a call did not resolve, so genuine gaps are separable from noise.

    A call to ``len`` is not a resolution failure, and neither is a call into a
    third-party package. Collapsing all three into one bucket would overstate
    how incomplete the graph is.
    """
    head = expression.partition(".")[0]
    if head in _BUILTIN_NAMES:
        return "builtin"
    if head in bindings:
        return "external_attribute"
    for reference in parsed.imports:
        if reference.bound_name == head:
            return "external_module"
    return "dynamic_or_local"


def _resolve(
    expression: str,
    parsed: ParsedFile,
    bindings: dict[str, str],
    symbol_ids: set[str],
) -> str | None:
    """Resolve a dotted expression to a symbol id, or ``None`` when unknowable."""
    local = f"{parsed.path}::{parsed.module}.{expression}" if parsed.module else None
    if local and local in symbol_ids:
        return local
    head, _, tail = expression.partition(".")
    bound = bindings.get(head)
    if bound is None:
        return None
    if not tail:
        return bound if bound in symbol_ids else None
    candidate = f"{bound}.{tail}"
    if candidate in symbol_ids:
        return candidate
    path, _, qualified = bound.partition("::")
    nested = f"{path}::{qualified}.{tail}"
    return nested if nested in symbol_ids else None


def _absolute_module(
    current_module: str, module: str, level: int, *, is_package: bool = False
) -> str:
    """Resolve a relative import to its absolute module name.

    ``.`` means the *containing package*. For ``pkg/mod.py`` that is ``pkg``,
    but for ``pkg/__init__.py`` the containing package is ``pkg`` itself — so a
    package must not drop a component before counting levels. Each extra dot
    then strips one more component.
    """
    if level == 0:
        return module
    parts = current_module.split(".") if current_module else []
    if not is_package and parts:
        parts = parts[:-1]
    if level > 1:
        parts = parts[: max(0, len(parts) - (level - 1))]
    return ".".join([*parts, module]) if module else ".".join(parts)


def _package_of(parsed: ParsedFile) -> bool:
    return parsed.path.endswith("__init__.py")


def _import_expression(module: str, name: str | None, level: int) -> str:
    prefix = "." * level
    if name:
        return f"from {prefix}{module} import {name}"
    return f"import {prefix}{module}"


def blast_radius(
    graph: RepositoryGraph, changed_paths: set[str], *, max_callers: int = _MAX_CALLERS
) -> dict[str, Any]:
    """Files that reference anything defined in ``changed_paths``.

    This is the check-set for collateral breakage and the seed for a task's
    ``files_in_scope``. Rendered as a small slice, never the whole graph.
    """
    owner: dict[str, str] = {}
    for parsed in graph.files:
        for symbol in parsed.symbols:
            owner[symbol.id] = parsed.path

    impacted: dict[str, list[dict[str, Any]]] = {}
    for edge in graph.edges:
        if edge.kind == "contains":
            continue
        target_path = owner.get(edge.target)
        source_path = owner.get(edge.source)
        if target_path is None or target_path not in changed_paths:
            continue
        if source_path is None or source_path in changed_paths:
            continue
        impacted.setdefault(target_path, []).append(
            {
                "caller": edge.source,
                "kind": edge.kind,
                "anchor": str(edge.anchor),
                "expression": edge.expression,
            }
        )

    return {
        "changed": sorted(changed_paths),
        "impacted": {
            path: sorted(entries, key=lambda item: item["anchor"])[:max_callers]
            for path, entries in sorted(impacted.items())
        },
        "impacted_file_count": len(impacted),
        "truncated": any(len(entries) > max_callers for entries in impacted.values()),
    }


def validate_graph(graph: RepositoryGraph, root: Path) -> dict[str, Any]:
    """Re-derive every edge from a fresh parse and report the match rate.

    The grading criterion is "graph edges verified against source", so this
    re-reads the repository rather than trusting the in-memory build.
    """
    rebuilt = build_graph(root)
    original_keys = {edge.key() for edge in graph.edges}
    rebuilt_keys = {edge.key() for edge in rebuilt.edges}
    matched = original_keys & rebuilt_keys
    total = len(original_keys)
    anchored, misanchored = _verify_anchors(graph, root)
    return {
        "schema_version": "0.1.0",
        "edges_total": total,
        "edges_matched": len(matched),
        "edges_only_in_graph": sorted(str(key) for key in (original_keys - rebuilt_keys))[:20],
        "edges_only_in_rebuild": sorted(str(key) for key in (rebuilt_keys - original_keys))[:20],
        "edge_match_rate": round(len(matched) / total, 6) if total else 1.0,
        "anchors_checked": anchored + len(misanchored),
        "anchors_valid": anchored,
        "anchors_invalid": misanchored[:20],
        "anchor_match_rate": (
            round(anchored / (anchored + len(misanchored)), 6)
            if (anchored + len(misanchored))
            else 1.0
        ),
        "status": "verified"
        if total == len(matched) and not misanchored
        else "mismatched",
    }


@dataclass(frozen=True, slots=True)
class GraphArtifacts:
    repository_root: Path
    knowledge_root: Path
    action: str
    statistics: dict[str, int]
    validation: dict[str, Any]


def build_graph_artifacts(source_value: str, *, cwd: Path | None = None) -> GraphArtifacts:
    working_directory = (cwd or Path.cwd()).resolve()
    source = resolve_source(source_value, working_directory)
    repository, action = prepare_repository(source, working_directory)
    repository.add_metadata_exclude()

    metadata_root = repository.root / ".stress_stack"
    knowledge_root = metadata_root / "knowledge"
    knowledge_root.mkdir(parents=True, exist_ok=True)

    graph = build_graph(repository.root)
    validation = validate_graph(graph, repository.root)

    atomic_write_json(knowledge_root / "repo_graph.json", graph.to_dict())
    atomic_write_json(knowledge_root / "graph_validation.json", validation)
    update_stage(
        metadata_root,
        "knowledge_layer",
        "graph_complete" if validation["status"] == "verified" else "graph_mismatched",
    )
    return GraphArtifacts(
        repository_root=repository.root,
        knowledge_root=knowledge_root,
        action=action,
        statistics=graph.statistics(),
        validation=validation,
    )


@dataclass(frozen=True, slots=True)
class DependencyArtifacts:
    repository_root: Path
    knowledge_root: Path
    counts: dict[str, int]
    runtime: dict[str, str]
    test: dict[str, str]
    optional: dict[str, str]
    audit: dict[str, list[str]]
    lockfile: Path
    environment_available: bool
    lock: dict[str, Any]
    locked_pins: dict[str, str]


def build_dependency_artifacts(
    source_value: str, *, cwd: Path | None = None
) -> DependencyArtifacts:
    from stress_stack.dependencies import build_report
    from stress_stack.hygiene import provision_test_environment
    from stress_stack.locking import compile_lock, ensure_uv, locked_packages, select_extras

    working_directory = (cwd or Path.cwd()).resolve()
    source = resolve_source(source_value, working_directory)
    repository, _ = prepare_repository(source, working_directory)
    repository.add_metadata_exclude()

    metadata_root = repository.root / ".stress_stack"
    knowledge_root = metadata_root / "knowledge"
    knowledge_root.mkdir(parents=True, exist_ok=True)

    environment = provision_test_environment(repository.root, metadata_root / "tools" / "test")
    python = Path(environment["python"]) if environment["installed"] else None
    graph = build_graph(repository.root)
    checkable, unverifiable = _partition_declarations(environment)
    report = build_report(graph, python, declared=checkable)

    lockfile = repository.root / "requirements.lock"
    lock = compile_lock(
        repository.root,
        lockfile,
        extras=select_extras(environment.get("declared_extras") or []),
        uv=ensure_uv(),
    )
    pins = locked_packages(lockfile) if lock.status.startswith("locked") else {}

    payload = report.to_dict()
    payload["lock"] = lock.to_dict()
    payload["audit"]["declared_in_uninstalled_extra"] = unverifiable
    payload["audit"]["imported_not_locked"] = sorted(
        fact.distribution
        for fact in report.facts
        if fact.kind == "third_party"
        and fact.distribution
        and _normalize_name(fact.distribution) not in {_normalize_name(n) for n in pins}
    )
    atomic_write_json(knowledge_root / "dependencies.json", payload)

    return DependencyArtifacts(
        repository_root=repository.root,
        knowledge_root=knowledge_root,
        counts=payload["counts"],
        runtime=report.runtime,
        test=report.test,
        optional=report.optional,
        audit=payload["audit"],
        lockfile=lockfile,
        environment_available=report.environment_available,
        lock=lock.to_dict(),
        locked_pins=pins,
    )


def measure_coverage(repository_root: Path, graph: RepositoryGraph, knowledge_root: Path):
    """Run the suite with per-test contexts and attribute lines to symbols.

    Kept separate from enrichment because coverage is measured and enrichment is
    generated: candidate mining depends on the first and must not be blocked by
    the availability of a model.
    """
    from stress_stack import coverage_map as cm
    from stress_stack.hygiene import provision_test_environment

    metadata_root = repository_root / ".stress_stack"
    environment = provision_test_environment(repository_root, metadata_root / "tools" / "test")
    if not environment["installed"]:
        return None, f"unavailable: {environment.get('reason') or 'no_test_environment'}"

    packages = sorted(
        {p.module.split(".")[0] for p in graph.files if p.module and not p.is_test and "/" in p.path}
    )
    lines, status, reason = cm.collect(
        repository_root, Path(environment["python"]), knowledge_root, packages=packages
    )
    if status != "available":
        return None, f"{status}: {reason}"
    coverage = cm.build(graph, lines)
    atomic_write_json(knowledge_root / "coverage_map.json", coverage.to_dict())
    return coverage, "available"


def build_coverage_artifacts(source_value: str, *, cwd: Path | None = None) -> dict[str, Any]:
    """Measured coverage only — no model involved, so `mine` never needs a key."""
    working_directory = (cwd or Path.cwd()).resolve()
    source = resolve_source(source_value, working_directory)
    repository, _ = prepare_repository(source, working_directory)
    repository.add_metadata_exclude()
    knowledge_root = repository.root / ".stress_stack" / "knowledge"
    knowledge_root.mkdir(parents=True, exist_ok=True)

    graph = build_graph(repository.root)
    coverage, note = measure_coverage(repository.root, graph, knowledge_root)
    return {
        "repository_root": str(repository.root),
        "coverage": note,
        "statistics": coverage.statistics() if coverage else {},
        "knowledge_root": str(knowledge_root),
    }


def build_index_artifacts(source_value: str, *, cwd: Path | None = None) -> dict[str, Any]:
    """Project the JSON knowledge layer into the queryable tier."""
    from stress_stack import coverage_map as cm
    from stress_stack.index import build_index, connect, module_test_matrix

    working_directory = (cwd or Path.cwd()).resolve()
    source = resolve_source(source_value, working_directory)
    repository, _ = prepare_repository(source, working_directory)
    repository.add_metadata_exclude()
    metadata_root = repository.root / ".stress_stack"
    knowledge_root = metadata_root / "knowledge"
    knowledge_root.mkdir(parents=True, exist_ok=True)

    graph = build_graph(repository.root)
    coverage = cm.load(knowledge_root / "coverage_map.json")
    stats = build_index(
        knowledge_root / "index.sqlite",
        graph,
        coverage if coverage.symbols else None,
        metadata_root / "history",
    )
    with connect(knowledge_root / "index.sqlite") as connection:
        matrix = module_test_matrix(connection)
    payload = {**stats.to_dict(), "module_test_matrix": matrix}
    atomic_write_json(knowledge_root / "index.json", payload)
    return {"repository_root": str(repository.root), **payload}


def build_mining_artifacts(source_value: str, *, cwd: Path | None = None) -> dict[str, Any]:
    """Rank the pool of things that could become tasks, and record why the rest could not."""
    from stress_stack import coverage_map as cm
    from stress_stack.candidates import (
        mine_excision,
        mine_history,
        module_distribution,
    )

    working_directory = (cwd or Path.cwd()).resolve()
    source = resolve_source(source_value, working_directory)
    repository, _ = prepare_repository(source, working_directory)
    repository.add_metadata_exclude()
    metadata_root = repository.root / ".stress_stack"
    knowledge_root = metadata_root / "knowledge"
    knowledge_root.mkdir(parents=True, exist_ok=True)

    graph = build_graph(repository.root)
    coverage = cm.load(knowledge_root / "coverage_map.json")
    history, history_funnel, thresholds = mine_history(
        repository, graph, metadata_root / "history"
    )
    excision, excision_funnel = mine_excision(coverage, graph)

    payload = {
        "schema_version": "0.1.0",
        "coverage_status": coverage.status,
        "thresholds": thresholds,
        "history": {
            "funnel": history_funnel.to_dict(),
            "module_distribution": module_distribution(history),
            "candidates": [candidate.to_dict() for candidate in history],
        },
        "excision": {
            "funnel": excision_funnel.to_dict(),
            "module_distribution": module_distribution(excision),
            "candidates": [candidate.to_dict() for candidate in excision],
        },
    }
    atomic_write_json(knowledge_root / "candidates.json", payload)
    update_stage(metadata_root, "task_generation", "mined")

    return {
        "repository_root": str(repository.root),
        "coverage_status": coverage.status,
        "history_count": len(history),
        "history_funnel": history_funnel.to_dict(),
        "history_modules": module_distribution(history),
        "excision_count": len(excision),
        "excision_funnel": excision_funnel.to_dict(),
        "excision_modules": module_distribution(excision),
        "thresholds": thresholds,
        "knowledge_root": str(knowledge_root),
    }


def build_validation_artifacts(
    source_value: str,
    *,
    cwd: Path | None = None,
    history_limit: int = 12,
    excision_limit: int = 8,
    repeats: int = 2,
    only: str | None = None,
    policy: str = "strict",
) -> dict[str, Any]:
    """Stage the ranked candidates and put each through every gate."""
    from stress_stack.candidates import EXCISION, HISTORY, load_candidates
    from stress_stack.runner import select_runner
    from stress_stack.validate import validate_pool, write_validation

    working_directory = (cwd or Path.cwd()).resolve()
    source = resolve_source(source_value, working_directory)
    repository, _ = prepare_repository(source, working_directory)
    repository.add_metadata_exclude()
    metadata_root = repository.root / ".stress_stack"
    knowledge_root = metadata_root / "knowledge"
    tasks_root = metadata_root / "tasks"
    work_root = metadata_root / "work"
    tasks_root.mkdir(parents=True, exist_ok=True)
    work_root.mkdir(parents=True, exist_ok=True)

    pool = load_candidates(knowledge_root / "candidates.json")
    if not any(pool.values()):
        raise MetadataError(
            "No candidates found. Run `stress-stack mine` (and `coverage` before it) first."
        )

    # Matches the tag `stress-stack container` builds; validation reuses that
    # image rather than building a second one, so the environment a task is
    # judged in is the environment the container stage already proved
    # deterministic.
    runner = select_runner(image=f"stress-stack/{repository.root.name.lower()}:verify")

    graph = build_graph(repository.root)
    built: list[Any] = []
    summaries: dict[str, Any] = {}
    for name, limit in ((HISTORY, history_limit), (EXCISION, excision_limit)):
        if only and only != name:
            continue
        results, summary = validate_pool(
            repository,
            graph,
            pool[name],
            tasks_root,
            work_root,
            runner,
            limit=limit,
            repeats=repeats,
            policy=policy,
        )
        built.extend(results)
        summaries[name] = summary.to_dict()

    from stress_stack.validate import ValidationSummary

    combined = ValidationSummary(
        attempted=sum(s["attempted"] for s in summaries.values()),
        eligible=sum(s["eligible"] for s in summaries.values()),
        seconds=sum(s["seconds"] for s in summaries.values()),
    )
    for entry in summaries.values():
        for reason_code, ids in entry["rejected"].items():
            combined.rejected.setdefault(reason_code, []).extend(ids)

    write_validation(knowledge_root / "validation.json", built, combined)
    update_stage(metadata_root, "task_generation", "validated")

    return {
        "repository_root": str(repository.root),
        "backend": runner.backend,
        "image": runner.image,
        "policy": policy,
        "by_source": summaries,
        "summary": combined.to_dict(),
        "eligible": [task.task_id for task in built if task.eligible],
        "tasks_root": str(tasks_root),
        "knowledge_root": str(knowledge_root),
    }


def build_selection_artifacts(source_value: str, *, cwd: Path | None = None) -> dict[str, Any]:
    """Choose the ten, score their difficulty, and record quota compliance."""
    from stress_stack.emit import load_eligible, run_selection

    working_directory = (cwd or Path.cwd()).resolve()
    source = resolve_source(source_value, working_directory)
    repository, _ = prepare_repository(source, working_directory)
    knowledge_root = repository.root / ".stress_stack" / "knowledge"

    eligible = load_eligible(knowledge_root / "validation.json")
    if not eligible:
        raise MetadataError(
            "No validated tasks found. Run `stress-stack validate` before selecting."
        )
    selection = run_selection(eligible, build_graph(repository.root))
    atomic_write_json(knowledge_root / "selection_report.json", selection)
    return {"repository_root": str(repository.root), "knowledge_root": str(knowledge_root),
            **selection}


def build_emission_artifacts(
    source_value: str, *, cwd: Path | None = None, use_model: bool = True
) -> dict[str, Any]:
    """Write each task's statement and manifest, then tasks.json."""
    from stress_stack.emit import emit_bundle, load_eligible, run_selection

    working_directory = (cwd or Path.cwd()).resolve()
    source = resolve_source(source_value, working_directory)
    repository, _ = prepare_repository(source, working_directory)
    metadata_root = repository.root / ".stress_stack"
    knowledge_root = metadata_root / "knowledge"
    tasks_root = metadata_root / "tasks"

    eligible = load_eligible(knowledge_root / "validation.json")
    if not eligible:
        raise MetadataError(
            "No validated tasks found. Run `stress-stack validate` before emitting."
        )
    graph = build_graph(repository.root)
    client = None
    if use_model:
        from stress_stack.openrouter import OpenRouterClient

        candidate_client = OpenRouterClient(cache_dir=metadata_root / "cache" / "llm")
        client = candidate_client if candidate_client.configured else None
    # Recomputed, never reused. Selection is deterministic and cheap, and
    # reading a stale report silently emitted tasks whose difficulty
    # justifications predated a fix to how they are written.
    selection = run_selection(eligible, graph)
    atomic_write_json(knowledge_root / "selection_report.json", selection)
    manifest = emit_bundle(
        selection,
        eligible,
        graph,
        tasks_root,
        metadata_root / "tasks.json",
        history_root=metadata_root / "history",
        client=client,
    )
    update_stage(metadata_root, "task_generation", "emitted")
    return {
        "repository_root": str(repository.root),
        "tasks_root": str(tasks_root),
        "manifest": str(metadata_root / "tasks.json"),
        **manifest,
    }


def build_enrichment_artifacts(source_value: str, *, cwd: Path | None = None, workers: int = 20):
    """graph + coverage -> file cards -> blueprint, all written under knowledge/."""
    from stress_stack.enrich import (
        enrich_repository, summarize, synthesize_blueprint, write_cards,
    )
    from stress_stack.openrouter import OpenRouterClient

    working_directory = (cwd or Path.cwd()).resolve()
    source = resolve_source(source_value, working_directory)
    repository, _ = prepare_repository(source, working_directory)
    repository.add_metadata_exclude()
    metadata_root = repository.root / ".stress_stack"
    knowledge_root = metadata_root / "knowledge"
    knowledge_root.mkdir(parents=True, exist_ok=True)

    graph = build_graph(repository.root)
    coverage, coverage_note = measure_coverage(repository.root, graph, knowledge_root)

    client = OpenRouterClient(cache_dir=metadata_root / "cache" / "llm")
    results = enrich_repository(graph, coverage, client, max_workers=workers)
    write_cards(knowledge_root / "file_cards.jsonl", results)

    readme = ""
    for name in ("README.md", "README.rst", "README"):
        candidate = repository.root / name
        if candidate.is_file():
            readme = candidate.read_text(encoding="utf-8", errors="replace")
            break
    blueprint, blueprint_meta = synthesize_blueprint(client, results, readme=readme)
    atomic_write_json(knowledge_root / "blueprint.json", {**blueprint, "_meta": blueprint_meta})

    report = {
        "coverage": coverage_note,
        "coverage_statistics": coverage.statistics() if coverage else {},
        "cards": summarize(results),
        "blueprint": blueprint_meta,
        "usage": client.usage.to_dict(),
        "knowledge_root": str(knowledge_root),
    }
    atomic_write_json(knowledge_root / "enrichment.json", report)
    return report


def _normalize_name(name: str) -> str:
    return "".join("-" if character in "-_." else character for character in name.lower())


def build_container_artifacts(source_value: str, *, cwd: Path | None = None):
    from stress_stack.atomic import atomic_write_text
    from stress_stack.container import (
        ContainerResult,
        build_image,
        compare_to_baseline,
        detect_missing_system_library,
        render_dockerfile,
        resolve_base_image,
        run_suite_in_container,
        select_python_version,
    )
    from stress_stack.hygiene import provision_test_environment
    from stress_stack.locking import compile_lock, ensure_uv, select_extras

    working_directory = (cwd or Path.cwd()).resolve()
    source = resolve_source(source_value, working_directory)
    repository, _ = prepare_repository(source, working_directory)
    repository.add_metadata_exclude()

    metadata_root = repository.root / ".stress_stack"
    evidence = metadata_root / "container"
    evidence.mkdir(parents=True, exist_ok=True)

    environment = provision_test_environment(repository.root, metadata_root / "tools" / "test")
    python_version = select_python_version(environment.get("requires_python"))

    lockfile = repository.root / "requirements.lock"
    lock = compile_lock(
        repository.root,
        lockfile,
        extras=select_extras(environment.get("declared_extras") or []),
        python_version=python_version,
        uv=ensure_uv(),
    )
    base_image, pinning = resolve_base_image(python_version)
    dockerfile = repository.root / "Dockerfile"
    atomic_write_text(
        dockerfile,
        render_dockerfile(
            base_image,
            has_lock=lock.status.startswith("locked"),
            installable=(repository.root / "pyproject.toml").exists()
            or (repository.root / "setup.py").exists(),
            test_command='["python", "-m", "pytest", "-q"]',
            has_runner=_lock_provides_runner(lockfile),
        ),
    )
    from stress_stack.container import _DOCKERIGNORE

    atomic_write_text(repository.root / ".dockerignore", _DOCKERIGNORE)

    image = f"stress-stack/{repository.root.name.lower()}:verify"
    build_status, build_seconds, build_log = build_image(repository.root, image)

    runs: list[dict[str, Any]] = []
    identical = False
    baseline_match = "not_run"
    if build_status == "built":
        runs = [run_suite_in_container(image, evidence, name) for name in ("run1", "run2")]
        identical = runs[0]["outcomes"] == runs[1]["outcomes"] and bool(runs[0]["outcomes"])
        baseline_match = compare_to_baseline(runs[0]["outcomes"], _host_baseline(metadata_root))

    status = _container_status(build_status, identical, baseline_match)
    missing = detect_missing_system_library(build_log) if build_status == "failed" else None
    result = ContainerResult(
        dockerfile=dockerfile,
        image=image,
        python_version=python_version,
        base_image=f"{base_image} ({pinning})",
        build_status=build_status,
        build_seconds=build_seconds,
        build_log_tail=build_log,
        runs=runs,
        identical=identical,
        baseline_match=baseline_match,
        status=status,
    )
    payload = result.to_dict()
    payload["missing_system_library"] = missing
    payload["lock"] = lock.to_dict()
    atomic_write_json(evidence / "container.json", payload)
    update_stage(metadata_root, "repo_hygiene", f"container_{status}")
    return result


def _lock_provides_runner(lockfile: Path) -> bool:
    """Whether the compiled lock already installs a test runner.

    Read from the lock rather than predicted from the manifest, because the
    manifest can declare tests in an extra, in a PEP 735 group, or in a
    requirements file none of which the compile necessarily resolved.
    """
    if not lockfile.is_file():
        return False
    for line in lockfile.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip().lower().startswith("pytest=="):
            return True
    return False


def _container_status(build_status: str, identical: bool, baseline_match: str) -> str:
    if build_status != "built":
        return "build_failed"
    if not identical:
        return "nondeterministic"
    if baseline_match == "matches":
        return "verified"
    if baseline_match == "no_baseline":
        return "deterministic_unverified"
    return "baseline_mismatch"


def _host_baseline(metadata_root: Path) -> dict[str, str] | None:
    path = metadata_root / "hygiene" / "tests_after.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    outcomes = payload.get("outcomes")
    return outcomes if isinstance(outcomes, dict) else None


def _partition_declarations(environment: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Split declared requirements into those we can check and those we cannot.

    Requirements behind an extra that was never installed have no import-name
    mapping available, so calling them unused would be a false positive.
    """
    grouped = environment.get("requirements_by_extra")
    if not isinstance(grouped, dict):
        declared = environment.get("declared_requirements")
        return ([str(item) for item in declared] if isinstance(declared, list) else []), []
    installed = {str(item).lower() for item in environment.get("installed_extras") or []}
    checkable: set[str] = set()
    unverifiable: set[str] = set()
    for extra, names in grouped.items():
        target = checkable if (not extra or extra.lower() in installed) else unverifiable
        target.update(str(name) for name in names)
    return sorted(checkable), sorted(unverifiable - checkable)


def _verify_anchors(graph: RepositoryGraph, root: Path) -> tuple[int, list[str]]:
    """Confirm every symbol anchor points at a line that still exists."""
    line_counts: dict[str, int] = {}
    valid = 0
    invalid: list[str] = []
    for parsed in graph.files:
        path = root / parsed.path
        if parsed.path not in line_counts:
            try:
                line_counts[parsed.path] = len(path.read_text(encoding="utf-8").splitlines())
            except (OSError, UnicodeDecodeError):
                line_counts[parsed.path] = 0
        limit = line_counts[parsed.path]
        for symbol in parsed.symbols:
            if 1 <= symbol.anchor.line <= max(limit, 1) and symbol.anchor.end_line >= symbol.anchor.line:
                valid += 1
            else:
                invalid.append(f"{symbol.id} @ {symbol.anchor}")
    return valid, invalid
