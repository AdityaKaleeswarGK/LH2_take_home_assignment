from __future__ import annotations

import posixpath
import re
from pathlib import PurePosixPath
from typing import Any

import yaml

from inverse_alpha.knowledge_models import RepositoryGraph

_MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def okf_document_path(source_path: str, is_test: bool) -> str:
    prefix = "tests" if is_test else "modules"
    return f"{prefix}/{source_path}.md"


def render_okf_bundle(
    graph: RepositoryGraph, verified_at: str, diagnostics: list[dict[str, Any]]
) -> dict[str, str]:
    graph_dict = graph.to_dict()
    nodes = {node["id"]: node for node in graph_dict["nodes"]}
    file_nodes = [node for node in graph_dict["nodes"] if node["kind"] == "file"]
    module_documents = {
        node["id"]: okf_document_path(node["path"], node["is_test"])
        for node in file_nodes
    }
    files: dict[str, str] = {}

    index_links = [
        f"- [{node['qualified_name']}]({module_documents[node['id']]})"
        for node in sorted(file_nodes, key=lambda item: item["path"])
    ]
    index_body = (
        "# Repository Knowledge Index\n\n## Modules\n\n" + "\n".join(index_links) + "\n"
    )
    files["index.md"] = _document(
        {
            "okf_version": "0.2",
            "type": "Knowledge Index",
            "title": "Repository Knowledge Index",
            "provenance": _provenance(verified_at),
        },
        index_body,
    )
    files["repository.md"] = _document(
        {
            "type": "Python Repository",
            "title": graph.repository.get("name", "Python Repository"),
            "provenance": _provenance(verified_at),
        },
        _repository_body(graph),
    )

    for file_node in sorted(file_nodes, key=lambda item: item["path"]):
        output_path = module_documents[file_node["id"]]
        files[output_path] = _document(
            {
                "type": "Python Test Module"
                if file_node["is_test"]
                else "Python Module",
                "title": file_node["qualified_name"],
                "resources": [f"repo:///{file_node['path']}"],
                "provenance": _provenance(verified_at),
            },
            _module_body(
                file_node,
                graph_dict,
                nodes,
                module_documents,
                output_path,
                diagnostics,
            ),
        )
    return files


def validate_okf_bundle(
    files: dict[str, str], graph: RepositoryGraph, source_paths: set[str]
) -> dict[str, Any]:
    errors: list[str] = []
    graph_dict = graph.to_dict()
    file_nodes = [node for node in graph_dict["nodes"] if node["kind"] == "file"]
    expected_documents = {
        okf_document_path(node["path"], node["is_test"]): node for node in file_nodes
    }
    if set(files) != {"index.md", "repository.md", *expected_documents}:
        errors.append("OKF document set does not match graph file nodes")

    for path, content in sorted(files.items()):
        frontmatter = _parse_frontmatter(path, content, errors)
        if path == "index.md" and frontmatter.get("okf_version") != "0.2":
            errors.append("index.md does not declare OKF v0.2")
        if (
            not isinstance(frontmatter.get("type"), str)
            or not frontmatter["type"].strip()
        ):
            errors.append(f"{path} has no non-empty concept type")
        for resource in frontmatter.get("resources", []):
            if not isinstance(resource, str) or not resource.startswith("repo:///"):
                errors.append(f"{path} has an invalid repository resource")
                continue
            resource_path = resource.removeprefix("repo:///")
            if (
                resource_path not in source_paths
                or PurePosixPath(resource_path).is_absolute()
            ):
                errors.append(
                    f"{path} has an unresolved repository resource: {resource}"
                )
        for link in _MARKDOWN_LINK.findall(content):
            if "://" in link or link.startswith("#"):
                continue
            target = str(PurePosixPath(path).parent.joinpath(link))
            normalized = posixpath.normpath(target)
            if normalized not in files:
                errors.append(f"{path} has an unresolved concept link: {link}")

    for path, node in expected_documents.items():
        content = files.get(path, "")
        if node["path"] not in content or node["content_hash"] not in content:
            errors.append(f"{path} does not agree with its graph file node")

    return {
        "schema_version": "0.2.0",
        "status": "valid" if not errors else "invalid",
        "document_count": len(files),
        "checks": {
            "frontmatter": not any(
                "frontmatter" in item or "concept type" in item for item in errors
            ),
            "document_graph_agreement": not any(
                "does not agree" in item or "document set" in item for item in errors
            ),
            "links": not any("concept link" in item for item in errors),
            "resources": not any("repository resource" in item for item in errors),
        },
        "errors": errors,
    }


def _provenance(verified_at: str) -> dict[str, list[dict[str, str]]]:
    return {
        "produced_by": [
            {"actor": "inverse-alpha/0.2.0", "timestamp": verified_at},
        ],
        "verified_by": [
            {
                "actor": "process:inverse-alpha-structural-validator",
                "timestamp": verified_at,
            }
        ],
    }


def _document(frontmatter: dict[str, Any], body: str) -> str:
    yaml_text = yaml.safe_dump(
        frontmatter,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    ).strip()
    return f"---\n{yaml_text}\n---\n\n{body.rstrip()}\n"


def _repository_body(graph: RepositoryGraph) -> str:
    repository = graph.repository
    statistics = graph.statistics
    source_roots = ", ".join(repository["source_roots"]) or "."
    test_roots = ", ".join(repository["test_roots"]) or "None detected"
    return (
        "# Repository\n\n"
        f"- Language: `{repository['language']}`\n"
        f"- HEAD: `{repository['head_sha']}`\n"
        f"- Source digest: `{repository['source_digest']}`\n"
        f"- Dirty working tree: `{str(repository['dirty_working_tree']).lower()}`\n"
        f"- Source roots: `{source_roots}`\n"
        f"- Test roots: `{test_roots}`\n"
        f"- Files: `{statistics['files']}`\n"
        f"- Symbols: `{statistics['symbols']}`\n"
        f"- Edges: `{statistics['edges']}`\n"
        f"- Unresolved references: `{statistics['unresolved_references']}`\n"
    )


def _module_body(
    file_node: dict[str, Any],
    graph: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
    module_documents: dict[str, str],
    output_path: str,
    diagnostics: list[dict[str, Any]],
) -> str:
    file_id = file_node["id"]
    contained = [
        edge
        for edge in graph["edges"]
        if edge["kind"] == "contains" and edge["source"] == file_id
    ]
    descendant_ids = {file_id, *(edge["target"] for edge in contained)}
    changed = True
    while changed:
        changed = False
        for edge in graph["edges"]:
            if (
                edge["kind"] == "contains"
                and edge["source"] in descendant_ids
                and edge["target"] not in descendant_ids
            ):
                descendant_ids.add(edge["target"])
                changed = True

    sections = [
        f"# {file_node['qualified_name']}",
        "## Source",
        f"- Path: `{file_node['path']}`",
        f"- SHA-256: `{file_node['content_hash']}`",
    ]
    symbols = sorted(
        (nodes[node_id] for node_id in descendant_ids if node_id != file_id),
        key=lambda item: (item["span"]["start_byte"], item["id"]),
    )
    sections.extend(["## Defined symbols", *_node_bullets(symbols)])

    for title, kinds, outgoing in (
        ("Imports", {"imports"}, True),
        ("Calls", {"calls"}, True),
        ("Called by", {"calls"}, False),
        ("Inheritance", {"inherits"}, True),
        ("Tests", {"tests"}, True),
        ("Tested by", {"tests"}, False),
    ):
        facts = []
        for edge in graph["edges"]:
            endpoint = edge["source"] if outgoing else edge["target"]
            if edge["kind"] in kinds and endpoint in descendant_ids:
                other = edge["target"] if outgoing else edge["source"]
                facts.append(
                    _edge_bullet(edge, nodes[other], module_documents, output_path)
                )
        sections.extend([f"## {title}", *(sorted(set(facts)) or ["- None"])])

    external_imports = []
    for edge in graph["edges"]:
        if edge["kind"] == "imports" and edge["source"] in descendant_ids:
            target = nodes[edge["target"]]
            if target["kind"] == "external_module":
                external_imports.append(f"- `{target['qualified_name']}`")
    sections.extend(
        ["## External imports", *(sorted(set(external_imports)) or ["- None"])]
    )

    file_diagnostics = [
        item for item in diagnostics if item.get("path") == file_node["path"]
    ]
    unresolved = [
        item
        for item in graph["unresolved_references"]
        if item["evidence_path"] == file_node["path"]
    ]
    diagnostic_lines = [
        f"- `{item['kind']}` `{item['expression']}`: {item['reason']} "
        f"(line {item['evidence_span']['start_line']})"
        for item in unresolved
    ]
    diagnostic_lines.extend(
        f"- `{item['code']}`: {item['message']}" for item in file_diagnostics
    )
    sections.extend(
        ["## Parse and resolution diagnostics", *(diagnostic_lines or ["- None"])]
    )
    return "\n\n".join(sections) + "\n"


def _node_bullets(nodes: list[dict[str, Any]]) -> list[str]:
    if not nodes:
        return ["- None"]
    return [
        f"- `{node['kind']}` `{node['qualified_name']}` "
        f"(lines {node['span']['start_line']}-{node['span']['end_line']})"
        for node in nodes
    ]


def _edge_bullet(
    edge: dict[str, Any],
    node: dict[str, Any],
    module_documents: dict[str, str],
    output_path: str,
) -> str:
    label = node["qualified_name"]
    target_file_id = node["id"] if node["kind"] == "file" else None
    if target_file_id is None and node.get("path") is not None:
        target_file_id = f"file:{node['path']}"
    if target_file_id in module_documents:
        source_parent = PurePosixPath(output_path).parent
        target = PurePosixPath(module_documents[target_file_id])
        relative = _relative_posix(source_parent, target)
        label = f"[{label}]({relative})"
    else:
        label = f"`{label}`"
    return f"- {label} (line {edge['evidence_span']['start_line']})"


def _relative_posix(source: PurePosixPath, target: PurePosixPath) -> str:
    source_parts = source.parts
    target_parts = target.parts
    common = 0
    for left, right in zip(source_parts, target_parts):
        if left != right:
            break
        common += 1
    parts = [".."] * (len(source_parts) - common) + list(target_parts[common:])
    return "/".join(parts) or target.name


def _parse_frontmatter(path: str, content: str, errors: list[str]) -> dict[str, Any]:
    if not content.startswith("---\n") or "\n---\n" not in content[4:]:
        errors.append(f"{path} has invalid YAML frontmatter")
        return {}
    yaml_text = content[4:].split("\n---\n", 1)[0]
    try:
        value = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        errors.append(f"{path} has invalid YAML frontmatter: {exc}")
        return {}
    if not isinstance(value, dict):
        errors.append(f"{path} frontmatter is not a mapping")
        return {}
    return value
