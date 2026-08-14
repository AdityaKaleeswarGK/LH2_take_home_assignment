from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RepositoryPurpose:
    text: str
    evidence_path: str
    evidence_line: int
    source_text_hash: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DeclaredCapability:
    id: str
    text: str
    evidence_path: str
    evidence_line: int
    source_text_hash: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EntryPoint:
    id: str
    name: str
    kind: str
    target: str
    evidence_path: str
    implementation_node_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["implementation_node_ids"] = list(self.implementation_node_ids)
        return value


@dataclass(frozen=True, slots=True)
class FeatureRecord:
    id: str
    name: str
    kind: str
    description: str
    module: str
    path: str
    paths: tuple[str, ...]
    implementation_node_ids: tuple[str, ...]
    public_symbols: tuple[str, ...]
    internal_dependencies: tuple[str, ...]
    static_test_evidence: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["paths"] = list(self.paths)
        value["implementation_node_ids"] = list(self.implementation_node_ids)
        value["public_symbols"] = list(self.public_symbols)
        value["internal_dependencies"] = list(self.internal_dependencies)
        return value


@dataclass(frozen=True, slots=True)
class TestCaseRecord:
    id: str
    symbol_id: str
    name: str
    qualified_name: str
    path: str
    framework: str
    span: dict[str, int]
    feature_ids: tuple[str, ...]
    evidence_level: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["feature_ids"] = list(self.feature_ids)
        return value


@dataclass(frozen=True, slots=True)
class FeatureTestLink:
    id: str
    feature_id: str
    test_id: str
    relationship: str
    graph_edge_id: str
    evidence_path: str
    evidence_span: dict[str, int]
    source_text_hash: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ContextArtifacts:
    features: dict[str, Any]
    test_cases: tuple[TestCaseRecord, ...]
    feature_test_links: tuple[FeatureTestLink, ...]
    blueprint: str
    test_map: str
    validation: dict[str, Any]
