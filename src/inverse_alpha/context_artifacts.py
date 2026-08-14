from __future__ import annotations

import ast
import hashlib
import re
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any

from inverse_alpha.context_markdown import (
    render_ascii_tree,
    render_blueprint,
    render_test_map,
)
from inverse_alpha.context_models import (
    ContextArtifacts,
    DeclaredCapability,
    EntryPoint,
    FeatureRecord,
    FeatureTestLink,
    RepositoryPurpose,
    TestCaseRecord,
)
from inverse_alpha.knowledge_models import RepositoryGraph, stable_reference_id
from inverse_alpha.python_parser import ParsedFile


def build_context_artifacts(
    *,
    repository_root: Path,
    repository_name: str,
    source_digest: str,
    repository_paths: list[str],
    parsed_files: list[ParsedFile],
    source_contents: dict[str, bytes],
    graph: RepositoryGraph,
) -> ContextArtifacts:
    graph_data = graph.to_dict()
    nodes = {node["id"]: node for node in graph_data["nodes"]}
    edges = graph_data["edges"]
    documentation = _documentation_context(repository_root, repository_paths)
    purpose = documentation["purpose"]
    declared_capabilities = documentation["declared_capabilities"]
    entry_points = _entry_points(repository_root, repository_paths, nodes)

    feature_drafts, node_features = _feature_drafts(
        parsed_files, source_contents, nodes, edges
    )
    test_cases, links = _test_inventory(parsed_files, nodes, edges, node_features)
    evidence_by_feature: dict[str, set[str]] = {}
    for link in links:
        evidence_by_feature.setdefault(link.feature_id, set()).add(link.relationship)

    features = tuple(
        FeatureRecord(
            **draft,
            static_test_evidence=_feature_evidence_status(
                evidence_by_feature.get(draft["id"], set())
            ),
        )
        for draft in feature_drafts
    )
    features_document = {
        "schema_version": "0.2.0",
        "repository": {
            "name": repository_name,
            "source_digest": source_digest,
        },
        "purpose": purpose.to_dict() if purpose is not None else None,
        "declared_capabilities": [item.to_dict() for item in declared_capabilities],
        "entry_points": [item.to_dict() for item in entry_points],
        "features": [item.to_dict() for item in features],
        "statistics": {
            "declared_capabilities": len(declared_capabilities),
            "entry_points": len(entry_points),
            "features": len(features),
            "test_files": len({item.path for item in test_cases}),
            "test_cases": len(test_cases),
            "feature_test_links": len(links),
        },
    }
    ascii_tree = render_ascii_tree(repository_name, repository_paths)
    blueprint = render_blueprint(
        repository_name=repository_name,
        purpose=purpose,
        declared_capabilities=declared_capabilities,
        entry_points=entry_points,
        features=features,
        test_cases=test_cases,
        links=links,
        ascii_tree=ascii_tree,
        edges=edges,
        nodes=nodes,
    )
    test_map = render_test_map(features, test_cases, links)
    validation = validate_context_artifacts(
        features_document=features_document,
        test_cases=test_cases,
        links=links,
        blueprint=blueprint,
        test_map=test_map,
        graph_data=graph_data,
        repository_paths=set(repository_paths),
        repository_root=repository_root,
    )
    return ContextArtifacts(
        features=features_document,
        test_cases=test_cases,
        feature_test_links=links,
        blueprint=blueprint,
        test_map=test_map,
        validation=validation,
    )


def aggregate_context_digest(repository_root: Path, repository_paths: list[str]) -> str:
    digest = hashlib.sha256()
    for value in sorted(set(repository_paths)):
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
        path = repository_root.joinpath(*PurePosixPath(value).parts)
        if _is_narrative_input(value) and path.is_file():
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(b"\0")
    return digest.hexdigest()


def validate_context_artifacts(
    *,
    features_document: dict[str, Any],
    test_cases: tuple[TestCaseRecord, ...],
    links: tuple[FeatureTestLink, ...],
    blueprint: str,
    test_map: str,
    graph_data: dict[str, Any],
    repository_paths: set[str],
    repository_root: Path,
) -> dict[str, Any]:
    errors: list[str] = []
    node_ids = {item["id"] for item in graph_data["nodes"]}
    edge_ids = {item["id"] for item in graph_data["edges"]}
    feature_records = features_document.get("features", [])
    feature_ids = [item.get("id") for item in feature_records]
    test_ids = [item.id for item in test_cases]
    link_ids = [item.id for item in links]

    if len(set(feature_ids)) != len(feature_ids):
        errors.append("feature ids are not unique")
    if len(set(test_ids)) != len(test_ids):
        errors.append("test ids are not unique")
    if len(set(link_ids)) != len(link_ids):
        errors.append("feature-test link ids are not unique")

    for feature in feature_records:
        if any(path not in repository_paths for path in feature.get("paths", [])):
            errors.append(f"feature path does not exist: {feature.get('id')}")
        for node_id in feature.get("implementation_node_ids", []):
            if node_id not in node_ids:
                errors.append(f"feature references unknown node: {feature.get('id')}")

    test_id_set = set(test_ids)
    feature_id_set = set(feature_ids)
    for test in test_cases:
        if test.symbol_id not in node_ids or test.path not in repository_paths:
            errors.append(f"test case has invalid source: {test.id}")
    for link in links:
        if link.feature_id not in feature_id_set or link.test_id not in test_id_set:
            errors.append(f"feature-test link has invalid endpoint: {link.id}")
        if link.graph_edge_id not in edge_ids:
            errors.append(f"feature-test link has invalid graph evidence: {link.id}")
        if link.evidence_path not in repository_paths:
            errors.append(f"feature-test link has invalid evidence path: {link.id}")

    root_text = str(repository_root)
    if root_text in blueprint or root_text in test_map:
        errors.append("generated markdown contains an absolute repository path")
    if "## Repository Structure" not in blueprint or "├──" not in blueprint:
        errors.append("blueprint has no ASCII repository structure")
    if "## Evidence Semantics" not in test_map:
        errors.append("test map has no evidence semantics")

    return {
        "schema_version": "0.2.0",
        "status": "valid" if not errors else "invalid",
        "counts": {
            "features": len(feature_records),
            "test_cases": len(test_cases),
            "feature_test_links": len(links),
        },
        "checks": {
            "unique_ids": not any("not unique" in item for item in errors),
            "valid_graph_references": not any(
                "unknown node" in item or "graph evidence" in item for item in errors
            ),
            "valid_paths": not any(
                "path" in item or "source" in item for item in errors
            ),
            "portable_markdown": not any("absolute" in item for item in errors),
            "required_sections": not any("has no" in item for item in errors),
        },
        "errors": errors,
    }


def _documentation_context(
    repository_root: Path, repository_paths: list[str]
) -> dict[str, Any]:
    readme_path = next(
        (
            value
            for value in sorted(repository_paths, key=lambda item: item.casefold())
            if len(PurePosixPath(value).parts) == 1
            and PurePosixPath(value).name.casefold()
            in {"readme", "readme.md", "readme.rst", "readme.txt"}
        ),
        None,
    )
    if readme_path is None:
        purpose = _pyproject_purpose(repository_root, repository_paths)
        return {"purpose": purpose, "declared_capabilities": ()}

    content = repository_root.joinpath(readme_path).read_text(
        encoding="utf-8", errors="replace"
    )
    lines = content.splitlines()
    purpose = _readme_purpose(readme_path, lines)
    capabilities = _readme_capabilities(readme_path, lines)
    if purpose is None:
        purpose = _pyproject_purpose(repository_root, repository_paths)
    return {"purpose": purpose, "declared_capabilities": capabilities}


def _readme_purpose(path: str, lines: list[str]) -> RepositoryPurpose | None:
    paragraph: list[str] = []
    start_line = 0
    fenced = False
    for index, raw in enumerate(lines, start=1):
        line = raw.strip()
        if line.startswith("```"):
            fenced = not fenced
            continue
        if fenced or _skip_readme_line(line):
            if paragraph:
                break
            continue
        if not line:
            if paragraph:
                break
            continue
        if not paragraph:
            start_line = index
        paragraph.append(line)
    if not paragraph:
        return None
    text = _plain_markdown(" ".join(paragraph))[:600].strip()
    if not text:
        return None
    return RepositoryPurpose(text, path, start_line, _text_hash(text))


def _readme_capabilities(path: str, lines: list[str]) -> tuple[DeclaredCapability, ...]:
    values: list[DeclaredCapability] = []
    in_section = False
    section_level = 0
    for index, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", stripped)
        if heading:
            level = len(heading.group(1))
            title = _plain_markdown(heading.group(2)).casefold()
            if in_section and level <= section_level:
                in_section = False
            if title in {"features", "capabilities", "what it does", "key features"}:
                in_section = True
                section_level = level
            continue
        if not in_section:
            continue
        bullet = re.match(r"^[-*+]\s+(.+)$", stripped)
        if bullet is None:
            continue
        text = _plain_markdown(bullet.group(1))[:300].strip()
        if not text:
            continue
        values.append(
            DeclaredCapability(
                id=stable_reference_id("declared-feature", path, index, text),
                text=text,
                evidence_path=path,
                evidence_line=index,
                source_text_hash=_text_hash(text),
            )
        )
    return tuple(values)


def _pyproject_purpose(
    repository_root: Path, repository_paths: list[str]
) -> RepositoryPurpose | None:
    if "pyproject.toml" not in repository_paths:
        return None
    path = repository_root / "pyproject.toml"
    try:
        content = path.read_text(encoding="utf-8")
        project = tomllib.loads(content).get("project", {})
    except (OSError, tomllib.TOMLDecodeError, TypeError):
        return None
    description = project.get("description")
    if not isinstance(description, str) or not description.strip():
        return None
    text = description.strip()[:600]
    line = next(
        (
            index
            for index, value in enumerate(content.splitlines(), start=1)
            if value.strip().startswith("description")
        ),
        1,
    )
    return RepositoryPurpose(text, "pyproject.toml", line, _text_hash(text))


def _entry_points(
    repository_root: Path,
    repository_paths: list[str],
    nodes: dict[str, dict[str, Any]],
) -> tuple[EntryPoint, ...]:
    values: dict[str, EntryPoint] = {}
    if "pyproject.toml" in repository_paths:
        try:
            config = tomllib.loads(
                (repository_root / "pyproject.toml").read_text(encoding="utf-8")
            )
        except (OSError, tomllib.TOMLDecodeError):
            config = {}
        project = config.get("project", {})
        groups = (
            ("console_script", project.get("scripts", {})),
            ("gui_script", project.get("gui-scripts", {})),
            (
                "console_script",
                config.get("tool", {}).get("poetry", {}).get("scripts", {}),
            ),
        )
        for kind, entries in groups:
            if not isinstance(entries, dict):
                continue
            for name, raw_target in sorted(entries.items()):
                target = _entry_point_target(raw_target)
                if target is None:
                    continue
                node_ids = tuple(_resolve_entry_point_nodes(target, nodes))
                entry_id = f"entrypoint:{kind}:{name}"
                values[entry_id] = EntryPoint(
                    entry_id,
                    str(name),
                    kind,
                    target,
                    "pyproject.toml",
                    node_ids,
                )
    for parsed_path in sorted(repository_paths):
        if PurePosixPath(parsed_path).name != "__main__.py":
            continue
        matching = sorted(
            node_id
            for node_id, node in nodes.items()
            if node.get("path") == parsed_path
        )
        entry_id = f"entrypoint:python_module:{parsed_path}"
        values[entry_id] = EntryPoint(
            entry_id,
            PurePosixPath(parsed_path).parent.as_posix(),
            "python_module",
            parsed_path,
            parsed_path,
            tuple(matching),
        )
    return tuple(values[key] for key in sorted(values))


def _feature_drafts(
    parsed_files: list[ParsedFile],
    source_contents: dict[str, bytes],
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    drafts: list[dict[str, Any]] = []
    node_features: dict[str, str] = {}
    path_features: dict[str, str] = {}
    modules: dict[str, list[ParsedFile]] = {}
    for parsed in parsed_files:
        if not parsed.is_test:
            modules.setdefault(parsed.module, []).append(parsed)

    for module, module_files in sorted(modules.items()):
        feature_id = f"feature:module:{module}"
        ordered_files = sorted(
            module_files,
            key=lambda item: (PurePosixPath(item.path).suffix == ".pyi", item.path),
        )
        paths = tuple(item.path for item in ordered_files)
        primary = ordered_files[0]
        for path in paths:
            path_features[path] = feature_id
        implementation_nodes = sorted(
            node_id for node_id, node in nodes.items() if node.get("path") in paths
        )
        for node_id in implementation_nodes:
            node_features[node_id] = feature_id
        public_symbols = tuple(
            sorted(
                {
                    f"{parsed.module}.{symbol.qualified_name}"
                    for parsed in ordered_files
                    for symbol in parsed.symbols
                    if symbol.parent is None and not symbol.name.startswith("_")
                }
            )
        )
        description = _module_description(
            source_contents[primary.path], module, public_symbols
        )
        drafts.append(
            {
                "id": feature_id,
                "name": module,
                "kind": "python_module",
                "description": description,
                "module": module,
                "path": primary.path,
                "paths": paths,
                "implementation_node_ids": tuple(implementation_nodes),
                "public_symbols": public_symbols,
                "internal_dependencies": (),
            }
        )

    dependencies: dict[str, set[str]] = {item["id"]: set() for item in drafts}
    for edge in edges:
        if edge["kind"] != "imports":
            continue
        source_feature = node_features.get(edge["source"])
        target_feature = node_features.get(edge["target"])
        if source_feature and target_feature and source_feature != target_feature:
            dependencies[source_feature].add(target_feature)
            continue
        target = nodes.get(edge["target"], {})
        target_path = target.get("path")
        if source_feature and isinstance(target_path, str):
            target_feature = path_features.get(target_path)
            if target_feature and source_feature != target_feature:
                dependencies[source_feature].add(target_feature)

    output = []
    for draft in sorted(drafts, key=lambda item: item["id"]):
        output.append(
            {
                **draft,
                "internal_dependencies": tuple(sorted(dependencies[draft["id"]])),
            }
        )
    return output, node_features


def _test_inventory(
    parsed_files: list[ParsedFile],
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    node_features: dict[str, str],
) -> tuple[tuple[TestCaseRecord, ...], tuple[FeatureTestLink, ...]]:
    candidates: list[tuple[ParsedFile, Any, str, str]] = []
    test_files: dict[str, list[tuple[ParsedFile, Any, str, str]]] = {}
    for parsed in parsed_files:
        if not parsed.is_test:
            continue
        for symbol in parsed.symbols:
            if symbol.kind not in {"function", "method"} or not symbol.name.startswith(
                "test"
            ):
                continue
            symbol_id = f"symbol:{parsed.module}:{symbol.qualified_name}"
            test_id = f"test:{parsed.path}::{symbol.qualified_name}"
            candidate = (parsed, symbol, symbol_id, test_id)
            candidates.append(candidate)
            test_files.setdefault(parsed.path, []).append(candidate)

    links: dict[str, FeatureTestLink] = {}
    features_by_test: dict[str, set[str]] = {item[3]: set() for item in candidates}
    relationships_by_test: dict[str, set[str]] = {item[3]: set() for item in candidates}
    test_id_by_symbol = {item[2]: item[3] for item in candidates}
    file_id_tests = {
        f"file:{path}": [item[3] for item in values]
        for path, values in test_files.items()
    }

    for edge in edges:
        feature_id = node_features.get(edge["target"])
        if feature_id is None:
            continue
        destinations: list[tuple[str, str]] = []
        direct_test_id = test_id_by_symbol.get(edge["source"])
        if direct_test_id is not None and edge["kind"] == "calls":
            destinations.append((direct_test_id, "statically_calls"))
        if edge["kind"] == "imports":
            destinations.extend(
                (test_id, "file_import_context")
                for test_id in file_id_tests.get(edge["source"], [])
            )
        for test_id, relationship in destinations:
            link_id = stable_reference_id(
                "feature-test", feature_id, test_id, relationship, edge["id"]
            )
            links[link_id] = FeatureTestLink(
                id=link_id,
                feature_id=feature_id,
                test_id=test_id,
                relationship=relationship,
                graph_edge_id=edge["id"],
                evidence_path=edge["evidence_path"],
                evidence_span=edge["evidence_span"],
                source_text_hash=edge["source_text_hash"],
            )
            features_by_test[test_id].add(feature_id)
            relationships_by_test[test_id].add(relationship)

    test_cases = []
    for parsed, symbol, symbol_id, test_id in sorted(
        candidates, key=lambda item: item[3]
    ):
        relationships = relationships_by_test[test_id]
        evidence_level = (
            "direct_static_call"
            if "statically_calls" in relationships
            else (
                "test_file_import_context"
                if "file_import_context" in relationships
                else "discovered_only"
            )
        )
        test_cases.append(
            TestCaseRecord(
                id=test_id,
                symbol_id=symbol_id,
                name=symbol.name,
                qualified_name=f"{parsed.module}.{symbol.qualified_name}",
                path=parsed.path,
                framework=_test_framework(parsed),
                span=symbol.span.to_dict(),
                feature_ids=tuple(sorted(features_by_test[test_id])),
                evidence_level=evidence_level,
            )
        )
    return tuple(test_cases), tuple(links[key] for key in sorted(links))


def _module_description(
    content: bytes, module: str, public_symbols: tuple[str, ...]
) -> str:
    try:
        tree = ast.parse(content.decode("utf-8"))
        docstring = ast.get_docstring(tree, clean=True)
    except (SyntaxError, UnicodeDecodeError, ValueError):
        docstring = None
    if docstring:
        summary = " ".join(docstring.strip().split())
        match = re.match(r"^(.+?[.!?])(?:\s|$)", summary)
        return (match.group(1) if match else summary)[:300]
    if public_symbols:
        names = ", ".join(value.rsplit(".", 1)[-1] for value in public_symbols[:6])
        suffix = ", and others" if len(public_symbols) > 6 else ""
        return f"Python module defining {names}{suffix}."
    return f"Python module `{module}` with no public top-level symbols detected."


def _test_framework(parsed: ParsedFile) -> str:
    modules = {item.module for item in parsed.imports if item.module}
    if "unittest" in modules:
        return "unittest"
    if "pytest" in modules:
        return "pytest"
    return "python_test_convention"


def _feature_evidence_status(relationships: set[str]) -> str:
    if "statically_calls" in relationships:
        return "direct_static_call"
    if "file_import_context" in relationships:
        return "test_file_import_context"
    return "no_static_test_evidence"


def _entry_point_target(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict) and isinstance(value.get("callable"), str):
        return value["callable"].strip() or None
    return None


def _resolve_entry_point_nodes(
    target: str, nodes: dict[str, dict[str, Any]]
) -> list[str]:
    normalized = target.split("[", 1)[0].strip().replace(":", ".")
    return sorted(
        node_id
        for node_id, node in nodes.items()
        if node.get("qualified_name") == normalized
        or (
            node.get("kind") == "file"
            and normalized.startswith(f"{node.get('qualified_name')}.")
        )
    )


def _skip_readme_line(line: str) -> bool:
    return bool(
        not line
        or line.startswith("#")
        or line.startswith("![")
        or line.startswith("[![")
        or line.startswith(">")
        or line.startswith("<")
        or line.startswith("---")
        or line.startswith("===")
    )


def _plain_markdown(value: str) -> str:
    value = re.sub(r"!\[([^]]*)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"[`*~]", "", value)
    return " ".join(value.split())


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_narrative_input(path_value: str) -> bool:
    name = PurePosixPath(path_value).name.casefold()
    return name in {
        "readme",
        "readme.md",
        "readme.rst",
        "readme.txt",
        "pyproject.toml",
    }
