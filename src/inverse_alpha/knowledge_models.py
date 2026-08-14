from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any, Literal, Protocol

from inverse_alpha.errors import MetadataError

NodeKind = Literal["file", "class", "function", "method", "external_module"]
EdgeKind = Literal["contains", "imports", "calls", "inherits", "decorated_by", "tests"]


@dataclass(frozen=True, slots=True)
class SourceSpan:
    start_byte: int
    end_byte: int
    start_line: int
    start_column: int
    end_line: int
    end_column: int

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class GraphNode:
    id: str
    kind: NodeKind
    name: str
    qualified_name: str
    path: str | None
    span: SourceSpan | None
    content_hash: str | None
    visibility: Literal["public", "private", "external"]
    is_test: bool

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        if self.span is not None:
            value["span"] = self.span.to_dict()
        return value


@dataclass(frozen=True, slots=True)
class GraphEdge:
    id: str
    kind: EdgeKind
    source: str
    target: str
    evidence_path: str
    evidence_span: SourceSpan
    source_text_hash: str
    extractor: str
    extractor_version: str
    resolution_status: Literal["resolved"] = "resolved"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["evidence_span"] = self.evidence_span.to_dict()
        return value


@dataclass(frozen=True, slots=True)
class UnresolvedReference:
    id: str
    kind: Literal["import", "call", "base", "decorator", "dynamic_import"]
    expression: str
    source: str
    evidence_path: str
    evidence_span: SourceSpan
    source_text_hash: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["evidence_span"] = self.evidence_span.to_dict()
        return value


@dataclass(frozen=True, slots=True)
class RepositoryGraph:
    repository: dict[str, Any]
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    unresolved_references: list[UnresolvedReference]
    statistics: dict[str, int]
    schema_version: str = "0.2.0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "repository": self.repository,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "unresolved_references": [
                item.to_dict() for item in self.unresolved_references
            ],
            "statistics": self.statistics,
        }


@dataclass(frozen=True, slots=True)
class KnowledgeAnnotation:
    id: str
    provider: str
    model: str
    prompt_hash: str
    source_node_ids: list[str]
    verification_state: Literal["unverified", "verified", "rejected"]
    content: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class KnowledgeEnricher(Protocol):
    def enrich(self, graph: RepositoryGraph) -> list[KnowledgeAnnotation]: ...


class NullKnowledgeEnricher:
    def enrich(self, graph: RepositoryGraph) -> list[KnowledgeAnnotation]:
        del graph
        return []


def stable_reference_id(prefix: str, *parts: object) -> str:
    payload = "\0".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(payload).hexdigest()[:24]}"


def validate_repository_graph(
    graph: dict[str, Any], source_contents: dict[str, bytes]
) -> dict[str, Any]:
    errors: list[str] = []
    node_ids: set[str] = set()
    edge_ids: set[str] = set()

    for node in graph.get("nodes", []):
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id:
            errors.append("node has no stable id")
            continue
        if node_id in node_ids:
            errors.append(f"duplicate node id: {node_id}")
        node_ids.add(node_id)
        path = node.get("path")
        if path is not None:
            if not isinstance(path, str) or PurePosixPath(path).is_absolute():
                errors.append(f"node has non-relative path: {node_id}")
            elif path not in source_contents:
                errors.append(f"node path does not exist: {node_id}")
            span = node.get("span")
            if span is not None and path in source_contents:
                _validate_span(span, source_contents[path], f"node {node_id}", errors)

    for edge in graph.get("edges", []):
        edge_id = edge.get("id")
        if not isinstance(edge_id, str) or not edge_id:
            errors.append("edge has no stable id")
            continue
        if edge_id in edge_ids:
            errors.append(f"duplicate edge id: {edge_id}")
        edge_ids.add(edge_id)
        if edge.get("source") not in node_ids or edge.get("target") not in node_ids:
            errors.append(f"edge has invalid endpoint: {edge_id}")
        path = edge.get("evidence_path")
        if path not in source_contents:
            errors.append(f"edge evidence path does not exist: {edge_id}")
            continue
        span = edge.get("evidence_span")
        if not isinstance(span, dict):
            errors.append(f"edge has no evidence span: {edge_id}")
            continue
        if _validate_span(span, source_contents[path], f"edge {edge_id}", errors):
            evidence = source_contents[path][span["start_byte"] : span["end_byte"]]
            if hashlib.sha256(evidence).hexdigest() != edge.get("source_text_hash"):
                errors.append(f"edge evidence hash mismatch: {edge_id}")

    for reference in graph.get("unresolved_references", []):
        path = reference.get("evidence_path")
        span = reference.get("evidence_span")
        if path not in source_contents or not isinstance(span, dict):
            errors.append(
                f"unresolved reference has invalid evidence: {reference.get('id')}"
            )
            continue
        if _validate_span(span, source_contents[path], "unresolved reference", errors):
            evidence = source_contents[path][span["start_byte"] : span["end_byte"]]
            if hashlib.sha256(evidence).hexdigest() != reference.get(
                "source_text_hash"
            ):
                errors.append(
                    f"unresolved evidence hash mismatch: {reference.get('id')}"
                )

    status = "valid" if not errors else "invalid"
    return {
        "schema_version": "0.2.0",
        "status": status,
        "checks": {
            "unique_node_ids": len(node_ids) == len(graph.get("nodes", [])),
            "unique_edge_ids": len(edge_ids) == len(graph.get("edges", [])),
            "valid_endpoints": not any("invalid endpoint" in item for item in errors),
            "valid_paths_and_spans": not any(
                phrase in item
                for item in errors
                for phrase in ("path", "span", "bounds")
            ),
            "matching_evidence_hashes": not any(
                "hash mismatch" in item for item in errors
            ),
        },
        "errors": errors,
    }


def _validate_span(
    span: dict[str, Any], content: bytes, label: str, errors: list[str]
) -> bool:
    start = span.get("start_byte")
    end = span.get("end_byte")
    if not isinstance(start, int) or not isinstance(end, int):
        errors.append(f"{label} has non-integer span")
        return False
    if start < 0 or end < start or end > len(content):
        errors.append(f"{label} span is outside source bounds")
        return False
    return True


def require_valid_graph(
    graph: dict[str, Any], source_contents: dict[str, bytes]
) -> dict[str, Any]:
    validation = validate_repository_graph(graph, source_contents)
    if validation["status"] != "valid":
        raise MetadataError(
            "Generated repository graph failed structural validation: "
            + "; ".join(validation["errors"])
        )
    return validation
