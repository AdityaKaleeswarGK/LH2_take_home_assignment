from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from inverse_alpha.context_models import (
    DeclaredCapability,
    EntryPoint,
    FeatureRecord,
    FeatureTestLink,
    RepositoryPurpose,
    TestCaseRecord,
)


def render_ascii_tree(repository_name: str, paths: list[str]) -> str:
    tree: dict[str, Any] = {}
    for value in sorted(set(paths)):
        current = tree
        parts = PurePosixPath(value).parts
        for index, part in enumerate(parts):
            if index == len(parts) - 1:
                current.setdefault(part, None)
            else:
                child = current.setdefault(part, {})
                if child is None:
                    break
                current = child

    lines = [f"{repository_name}/"]

    def visit(children: dict[str, Any], prefix: str) -> None:
        ordered = sorted(
            children.items(),
            key=lambda item: (item[1] is None, item[0].casefold(), item[0]),
        )
        for index, (name, child) in enumerate(ordered):
            last = index == len(ordered) - 1
            connector = "└── " if last else "├── "
            suffix = "/" if isinstance(child, dict) else ""
            lines.append(f"{prefix}{connector}{name}{suffix}")
            if isinstance(child, dict):
                visit(child, prefix + ("    " if last else "│   "))

    visit(tree, "")
    return "\n".join(lines)


def render_blueprint(
    *,
    repository_name: str,
    purpose: RepositoryPurpose | None,
    declared_capabilities: tuple[DeclaredCapability, ...],
    entry_points: tuple[EntryPoint, ...],
    features: tuple[FeatureRecord, ...],
    test_cases: tuple[TestCaseRecord, ...],
    links: tuple[FeatureTestLink, ...],
    ascii_tree: str,
    edges: list[dict[str, Any]],
    nodes: dict[str, dict[str, Any]],
) -> str:
    lines = [
        "# Repository Blueprint",
        "",
        (
            "This document is a deterministic repository view. Descriptions come "
            "from repository metadata, README text, module docstrings, and verified "
            "structural extraction; unknown semantics are not guessed."
        ),
        "",
        "## Purpose",
        "",
    ]
    if purpose is None:
        lines.append(
            "No repository purpose was found in a root README or pyproject description."
        )
    else:
        lines.extend(
            [
                purpose.text,
                "",
                f"Evidence: `{purpose.evidence_path}:{purpose.evidence_line}`",
            ]
        )
    lines.extend(["", "## Declared Capabilities", ""])
    if declared_capabilities:
        lines.extend(
            f"- {item.text} (`{item.evidence_path}:{item.evidence_line}`)"
            for item in declared_capabilities
        )
    else:
        lines.append(
            "No explicit Features or Capabilities list was found in the root README."
        )

    lines.extend(["", "## Entry Points", ""])
    if entry_points:
        lines.extend(["| Name | Kind | Target | Evidence |", "|---|---|---|---|"])
        lines.extend(
            f"| {_cell(item.name)} | `{item.kind}` | `{_cell(item.target)}` | `{item.evidence_path}` |"
            for item in entry_points
        )
    else:
        lines.append("No package or module entry points were detected.")

    lines.extend(["", "## Repository Structure", "", "```text", ascii_tree, "```", ""])
    lines.extend(["## Python Components", ""])
    if features:
        lines.extend(
            [
                "| Feature ID | Module | Responsibility | Public symbols | Static test evidence |",
                "|---|---|---|---|---|",
            ]
        )
        for item in features:
            symbols = (
                ", ".join(f"`{value}`" for value in item.public_symbols)
                or "None detected"
            )
            lines.append(
                f"| `{item.id}` | `{item.module}` | {_cell(item.description)} | "
                f"{symbols} | `{item.static_test_evidence}` |"
            )
    else:
        lines.append("No non-test Python components were detected.")

    lines.extend(["", "## Internal File Connections", ""])
    connections = _file_connections(edges, nodes)
    if connections:
        lines.extend(
            f"- `{source}` imports `{target}` (`{path}:{line}`)"
            for source, target, path, line in connections
        )
    else:
        lines.append("No resolved internal file imports were detected.")

    lines.extend(
        [
            "",
            "## Test Overview",
            "",
            f"- Discovered test cases: `{len(test_cases)}`",
            f"- Static feature-test links: `{len(links)}`",
            "- Runtime coverage: `not_collected`",
            "- Mutation evidence: `not_collected`",
            "",
            "See [test_map.md](test_map.md) for the test-by-feature view.",
            "",
            "## Interpretation Limits",
            "",
            "- A Python module is treated as a source-grounded feature area in this first version.",
            "- Static test references do not prove that behavior executes or is asserted at runtime.",
            "- Dynamic dispatch and reflective relationships remain unresolved rather than guessed.",
            "",
        ]
    )
    return "\n".join(lines)


def render_test_map(
    features: tuple[FeatureRecord, ...],
    test_cases: tuple[TestCaseRecord, ...],
    links: tuple[FeatureTestLink, ...],
) -> str:
    tests_by_id = {item.id: item for item in test_cases}
    links_by_feature: dict[str, list[FeatureTestLink]] = {}
    for link in links:
        links_by_feature.setdefault(link.feature_id, []).append(link)
    test_files = sorted({item.path for item in test_cases})
    direct_count = sum(item.relationship == "statically_calls" for item in links)
    import_count = sum(item.relationship == "file_import_context" for item in links)
    lines = [
        "# Test Map",
        "",
        "This document inventories conventionally discovered Python test functions and maps them to source feature areas using verified static graph evidence.",
        "",
        "## Summary",
        "",
        f"- Test files containing discovered cases: `{len(test_files)}`",
        f"- Discovered test cases: `{len(test_cases)}`",
        f"- Direct static call links: `{direct_count}`",
        f"- Test-file import-context links: `{import_count}`",
        "- Runtime coverage: `not_collected`",
        "- Mutation verification: `not_collected`",
        "",
        "## Evidence Semantics",
        "",
        "- `direct_static_call`: the test function contains a call resolved to the feature area.",
        "- `test_file_import_context`: the test file imports the feature area; this does not prove that a particular test executes it.",
        "- `discovered_only`: the test function was discovered, but no source feature was statically mapped.",
        "- None of these statuses is runtime coverage or proof of an effective assertion.",
        "",
        "## Feature-to-Test Map",
        "",
        "| Feature | Static evidence | Related test cases |",
        "|---|---|---|",
    ]
    for feature in features:
        feature_links = links_by_feature.get(feature.id, [])
        related_ids = sorted({item.test_id for item in feature_links})
        related = ", ".join(f"`{tests_by_id[item].name}`" for item in related_ids)
        lines.append(
            f"| `{feature.id}` | `{feature.static_test_evidence}` | {related or 'None'} |"
        )

    lines.extend(["", "## Test Cases by File", ""])
    if not test_cases:
        lines.append("No conventionally named Python test functions were discovered.")
    for path in test_files:
        lines.extend([f"### `{path}`", ""])
        for test in (item for item in test_cases if item.path == path):
            features_text = ", ".join(f"`{item}`" for item in test.feature_ids)
            lines.append(
                f"- `{test.qualified_name}` (line {test.span['start_line']}; "
                f"evidence: `{test.evidence_level}`; features: {features_text or 'unmapped'})"
            )
        lines.append("")

    gaps = [
        item
        for item in features
        if item.static_test_evidence == "no_static_test_evidence"
    ]
    lines.extend(["## Static Evidence Gaps", ""])
    if gaps:
        lines.extend(
            f"- `{item.id}` (`{item.path}`) has no mapped static test evidence."
            for item in gaps
        )
    else:
        lines.append(
            "Every detected feature area has at least one static test relationship."
        )
    lines.extend(
        [
            "",
            "A static gap is a candidate for further analysis, not proof that the behavior is untested. Runtime coverage and mutation checks are required before making that claim.",
            "",
        ]
    )
    return "\n".join(lines)


def _file_connections(
    edges: list[dict[str, Any]], nodes: dict[str, dict[str, Any]]
) -> list[tuple[str, str, str, int]]:
    values: set[tuple[str, str, str, int]] = set()
    for edge in edges:
        if edge["kind"] != "imports":
            continue
        source = nodes.get(edge["source"], {})
        target = nodes.get(edge["target"], {})
        source_path = source.get("path")
        target_path = target.get("path")
        if not isinstance(source_path, str) or not isinstance(target_path, str):
            continue
        if source_path == target_path:
            continue
        values.add(
            (
                source_path,
                target_path,
                edge["evidence_path"],
                edge["evidence_span"]["start_line"],
            )
        )
    return sorted(values)


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")
