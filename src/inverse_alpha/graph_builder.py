from __future__ import annotations

import ast
import builtins
import hashlib
import sys
from collections.abc import Callable
from pathlib import PurePosixPath

from inverse_alpha.knowledge_models import (
    GraphEdge,
    GraphNode,
    RepositoryGraph,
    SourceSpan,
    UnresolvedReference,
    stable_reference_id,
)
from inverse_alpha.python_parser import (
    EXTRACTOR_NAME,
    EXTRACTOR_VERSION,
    ImportReference,
    ParsedFile,
)


def build_repository_graph(
    parsed_files: list[ParsedFile],
    source_contents: dict[str, bytes],
    *,
    repository_name: str,
    head_sha: str,
    source_digest: str,
    dirty: bool,
    source_roots: list[str],
    test_roots: list[str],
) -> RepositoryGraph:
    nodes: dict[str, GraphNode] = {}
    edges: dict[str, GraphEdge] = {}
    unresolved: dict[str, UnresolvedReference] = {}
    module_files: dict[str, str] = {}
    file_ids: dict[str, str] = {}
    symbol_ids: dict[tuple[str, str], str] = {}

    for parsed in parsed_files:
        file_id = f"file:{parsed.path}"
        file_ids[parsed.path] = file_id
        module_files.setdefault(parsed.module, file_id)
        nodes[file_id] = GraphNode(
            id=file_id,
            kind="file",
            name=PurePosixPath(parsed.path).name,
            qualified_name=parsed.module,
            path=parsed.path,
            span=None,
            content_hash=parsed.content_hash,
            visibility="public",
            is_test=parsed.is_test,
        )
        for symbol in parsed.symbols:
            symbol_id = f"symbol:{parsed.module}:{symbol.qualified_name}"
            symbol_ids[(parsed.module, symbol.qualified_name)] = symbol_id
            kind = (
                symbol.kind
                if symbol.kind in {"class", "function", "method"}
                else "function"
            )
            nodes[symbol_id] = GraphNode(
                id=symbol_id,
                kind=kind,
                name=symbol.name,
                qualified_name=f"{parsed.module}.{symbol.qualified_name}",
                path=parsed.path,
                span=symbol.span,
                content_hash=parsed.content_hash,
                visibility=_visibility(symbol.name),
                is_test=parsed.is_test,
            )

    def ensure_external(module: str) -> str:
        top_level = module.split(".", 1)[0] or module
        node_id = f"external:{top_level}"
        if node_id not in nodes:
            nodes[node_id] = GraphNode(
                id=node_id,
                kind="external_module",
                name=top_level,
                qualified_name=top_level,
                path=None,
                span=None,
                content_hash=None,
                visibility="external",
                is_test=False,
            )
        return node_id

    for parsed in parsed_files:
        content = source_contents[parsed.path]
        file_id = file_ids[parsed.path]
        for symbol in parsed.symbols:
            target = symbol_ids[(parsed.module, symbol.qualified_name)]
            source = file_id
            if symbol.parent is not None:
                source = symbol_ids.get((parsed.module, symbol.parent), file_id)
            _add_edge(
                edges, "contains", source, target, parsed.path, symbol.span, content
            )

    internal_tops = {module.split(".", 1)[0] for module in module_files}
    builtin_names = set(dir(builtins))
    for parsed in parsed_files:
        content = source_contents[parsed.path]
        file_id = file_ids[parsed.path]
        aliases: dict[str, tuple[str, str]] = {}
        for reference in parsed.imports:
            resolved_module = absolute_import_module(parsed, reference)
            target, alias_binding = _resolve_import(
                reference,
                resolved_module,
                module_files,
                symbol_ids,
                internal_tops,
                ensure_external,
            )
            if target is None:
                _add_unresolved(
                    unresolved,
                    "import",
                    _import_expression(reference),
                    file_id,
                    parsed.path,
                    reference.span,
                    content,
                    (
                        "wildcard_import_is_ambiguous"
                        if reference.name == "*"
                        else "internal_import_target_not_defined"
                    ),
                )
                continue
            _add_edge(
                edges, "imports", file_id, target, parsed.path, reference.span, content
            )
            aliases[reference.alias] = alias_binding

        for symbol in parsed.symbols:
            symbol_id = symbol_ids[(parsed.module, symbol.qualified_name)]
            for base in symbol.bases:
                _resolve_fact(
                    edges,
                    unresolved,
                    kind="inherits",
                    unresolved_kind="base",
                    expression=base.expression,
                    source=symbol_id,
                    path=parsed.path,
                    span=base.span,
                    content=content,
                    parsed=parsed,
                    scope=symbol.qualified_name,
                    aliases=aliases,
                    module_files=module_files,
                    symbol_ids=symbol_ids,
                    ensure_external=ensure_external,
                    builtin_names=builtin_names,
                )
            for decorator in symbol.decorators:
                _resolve_fact(
                    edges,
                    unresolved,
                    kind="decorated_by",
                    unresolved_kind="decorator",
                    expression=decorator.expression,
                    source=symbol_id,
                    path=parsed.path,
                    span=decorator.span,
                    content=content,
                    parsed=parsed,
                    scope=symbol.qualified_name,
                    aliases=aliases,
                    module_files=module_files,
                    symbol_ids=symbol_ids,
                    ensure_external=ensure_external,
                    builtin_names=builtin_names,
                )

        for call in parsed.calls:
            source = symbol_ids.get((parsed.module, call.scope), file_id)
            if call.expression == "__import__" or call.expression.endswith(
                ".import_module"
            ):
                _add_unresolved(
                    unresolved,
                    "dynamic_import",
                    call.expression,
                    source,
                    parsed.path,
                    call.span,
                    content,
                    "runtime_import_target_is_dynamic",
                )
                continue
            target = resolve_expression(
                call.expression,
                parsed,
                call.scope,
                aliases,
                module_files,
                symbol_ids,
                ensure_external,
                builtin_names,
            )
            if target is None:
                _add_unresolved(
                    unresolved,
                    "call",
                    call.expression,
                    source,
                    parsed.path,
                    call.span,
                    content,
                    "call_target_is_dynamic_or_ambiguous",
                )
            else:
                _add_edge(
                    edges, "calls", source, target, parsed.path, call.span, content
                )

    node_test_status = {node_id: node.is_test for node_id, node in nodes.items()}
    for edge in list(edges.values()):
        if edge.kind not in {"imports", "calls"}:
            continue
        if node_test_status.get(edge.source, False) and not node_test_status.get(
            edge.target, False
        ):
            _add_edge(
                edges,
                "tests",
                edge.source,
                edge.target,
                edge.evidence_path,
                edge.evidence_span,
                source_contents[edge.evidence_path],
            )

    node_values = sorted(nodes.values(), key=lambda item: item.id)
    edge_values = sorted(edges.values(), key=lambda item: item.id)
    unresolved_values = sorted(unresolved.values(), key=lambda item: item.id)
    statistics = {
        "files": sum(node.kind == "file" for node in node_values),
        "symbols": sum(
            node.kind in {"class", "function", "method"} for node in node_values
        ),
        "external_modules": sum(node.kind == "external_module" for node in node_values),
        "edges": len(edge_values),
        "imports": sum(edge.kind == "imports" for edge in edge_values),
        "calls": sum(edge.kind == "calls" for edge in edge_values),
        "inherits": sum(edge.kind == "inherits" for edge in edge_values),
        "decorators": sum(edge.kind == "decorated_by" for edge in edge_values),
        "tests": sum(edge.kind == "tests" for edge in edge_values),
        "unresolved_references": len(unresolved_values),
    }
    return RepositoryGraph(
        repository={
            "name": repository_name,
            "head_sha": head_sha,
            "source_digest": source_digest,
            "dirty_working_tree": dirty,
            "language": "python",
            "source_roots": source_roots,
            "test_roots": test_roots,
        },
        nodes=node_values,
        edges=edge_values,
        unresolved_references=unresolved_values,
        statistics=statistics,
    )


def _resolve_import(
    reference: ImportReference,
    module: str,
    module_files: dict[str, str],
    symbol_ids: dict[tuple[str, str], str],
    internal_tops: set[str],
    ensure_external: Callable[[str], str],
) -> tuple[str | None, tuple[str, str]]:
    if reference.name == "*":
        return None, ("module", module)
    if reference.name:
        child_module = ".".join(filter(None, (module, reference.name)))
        if child_module in module_files:
            return module_files[child_module], ("module", child_module)
        symbol_id = symbol_ids.get((module, reference.name))
        if symbol_id is not None:
            return symbol_id, ("symbol", symbol_id)
        if module.split(".", 1)[0] not in internal_tops:
            external_id = ensure_external(module or reference.name)
            return external_id, ("external", external_id)
        return None, ("module", module)
    if module in module_files:
        bound_module = (
            module
            if reference.alias != module.split(".", 1)[0]
            else module.split(".", 1)[0]
        )
        return module_files[module], ("module", bound_module)
    if module.split(".", 1)[0] not in internal_tops:
        external_id = ensure_external(module)
        return external_id, ("external", external_id)
    return None, ("module", module)


def _resolve_fact(
    edges: dict[str, GraphEdge],
    unresolved: dict[str, UnresolvedReference],
    *,
    kind: str,
    unresolved_kind: str,
    expression: str,
    source: str,
    path: str,
    span: SourceSpan,
    content: bytes,
    parsed: ParsedFile,
    scope: str | None,
    aliases: dict[str, tuple[str, str]],
    module_files: dict[str, str],
    symbol_ids: dict[tuple[str, str], str],
    ensure_external: Callable[[str], str],
    builtin_names: set[str],
) -> None:
    target = resolve_expression(
        expression,
        parsed,
        scope,
        aliases,
        module_files,
        symbol_ids,
        ensure_external,
        builtin_names,
    )
    if target is None:
        _add_unresolved(
            unresolved,
            unresolved_kind,
            expression,
            source,
            path,
            span,
            content,
            f"{unresolved_kind}_could_not_be_resolved_conservatively",
        )
    else:
        _add_edge(edges, kind, source, target, path, span, content)


def resolve_expression(
    expression: str,
    parsed: ParsedFile,
    scope: str | None,
    aliases: dict[str, tuple[str, str]],
    module_files: dict[str, str],
    symbol_ids: dict[tuple[str, str], str],
    ensure_external: Callable[[str], str],
    builtin_names: set[str],
) -> str | None:
    parts = _expression_parts(expression)
    if not parts:
        return None
    if parts[0] == "self" and scope:
        class_prefix = _enclosing_class(parsed, scope)
        if class_prefix and len(parts) == 2:
            return symbol_ids.get((parsed.module, f"{class_prefix}.{parts[1]}"))
        return None
    if parts[0] in aliases:
        kind, value = aliases[parts[0]]
        if kind == "symbol":
            return value if len(parts) == 1 else None
        if kind == "external":
            return value
        module = value
        if len(parts) > 1:
            parent_module = (
                ".".join([module, *parts[1:-1]]) if len(parts) > 2 else module
            )
            candidate_symbol = symbol_ids.get((parent_module, parts[-1]))
            if candidate_symbol:
                return candidate_symbol
            full_module = ".".join([module, *parts[1:]])
            if full_module in module_files:
                return module_files[full_module]
        if module in module_files:
            return module_files[module]
        return ensure_external(module)
    if len(parts) == 1:
        name = parts[0]
        if scope:
            scope_parts = scope.split(".")
            for length in range(len(scope_parts), 0, -1):
                candidate = ".".join([*scope_parts[:length], name])
                target = symbol_ids.get((parsed.module, candidate))
                if target:
                    return target
        target = symbol_ids.get((parsed.module, name))
        if target:
            return target
        if name in builtin_names:
            return ensure_external("builtins")
        return None
    target = symbol_ids.get((parsed.module, ".".join(parts)))
    if target:
        return target
    if parts[0] in sys.stdlib_module_names:
        return ensure_external(parts[0])
    return None


def absolute_import_module(parsed: ParsedFile, reference: ImportReference) -> str:
    module = reference.module or ""
    if reference.level == 0:
        return module
    is_package = PurePosixPath(parsed.path).name in {"__init__.py", "__init__.pyi"}
    package = parsed.module if is_package else parsed.module.rpartition(".")[0]
    parts = package.split(".") if package else []
    remove = max(reference.level - 1, 0)
    if remove > len(parts):
        return module
    base = parts[: len(parts) - remove]
    return ".".join([*base, *([module] if module else [])])


def _expression_parts(expression: str) -> list[str] | None:
    try:
        value = ast.parse(expression, mode="eval").body
    except SyntaxError:
        return None
    while isinstance(value, (ast.Call, ast.Subscript)):
        value = value.func if isinstance(value, ast.Call) else value.value
    parts: list[str] = []
    while isinstance(value, ast.Attribute):
        parts.append(value.attr)
        value = value.value
    if not isinstance(value, ast.Name):
        return None
    parts.append(value.id)
    return list(reversed(parts))


def _enclosing_class(parsed: ParsedFile, scope: str) -> str | None:
    candidates = [
        symbol.qualified_name
        for symbol in parsed.symbols
        if symbol.kind == "class"
        and (
            scope == symbol.qualified_name
            or scope.startswith(symbol.qualified_name + ".")
        )
    ]
    return max(candidates, key=len, default=None)


def _add_edge(
    edges: dict[str, GraphEdge],
    kind: str,
    source: str,
    target: str,
    path: str,
    span: SourceSpan,
    content: bytes,
) -> None:
    edge_id = stable_reference_id(
        "edge", kind, source, target, path, span.start_byte, span.end_byte
    )
    valid_kind = (
        kind
        if kind in {"contains", "imports", "calls", "inherits", "decorated_by", "tests"}
        else "calls"
    )
    edges[edge_id] = GraphEdge(
        id=edge_id,
        kind=valid_kind,
        source=source,
        target=target,
        evidence_path=path,
        evidence_span=span,
        source_text_hash=hashlib.sha256(
            content[span.start_byte : span.end_byte]
        ).hexdigest(),
        extractor=EXTRACTOR_NAME,
        extractor_version=EXTRACTOR_VERSION,
    )


def _add_unresolved(
    unresolved: dict[str, UnresolvedReference],
    kind: str,
    expression: str,
    source: str,
    path: str,
    span: SourceSpan,
    content: bytes,
    reason: str,
) -> None:
    item_id = stable_reference_id(
        "unresolved", kind, expression, source, path, span.start_byte, span.end_byte
    )
    valid_kind = (
        kind
        if kind in {"import", "call", "base", "decorator", "dynamic_import"}
        else "call"
    )
    unresolved[item_id] = UnresolvedReference(
        id=item_id,
        kind=valid_kind,
        expression=expression,
        source=source,
        evidence_path=path,
        evidence_span=span,
        source_text_hash=hashlib.sha256(
            content[span.start_byte : span.end_byte]
        ).hexdigest(),
        reason=reason,
    )


def _visibility(name: str) -> str:
    return "private" if name.startswith("_") and not name.startswith("__") else "public"


def _import_expression(reference: ImportReference) -> str:
    if reference.name:
        return f"from {'.' * reference.level}{reference.module or ''} import {reference.name}"
    return f"import {reference.module}"
