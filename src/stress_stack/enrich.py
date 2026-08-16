"""Semantic enrichment: file cards, then a repository blueprint.

Structure gives you symbols and edges. It cannot tell you that a repository
implements glob-style path matching with fallback semantics — that sentence is
irreducibly semantic, and trying to derive it from clustering thresholds is how
a pipeline drifts into repo-specific heuristics that fail on a held-out repo.

Three properties keep the generated layer honest:

* **Waves, not a flat fan-out.** Files are described in dependency order, so a
  file is described knowing what it is built on. glom's import DAG is six waves
  deep with no cycles, and a wave costs one round regardless of its size.
* **Grounding.** Every symbol a model names must resolve to a real entity id.
  An ungrounded card is rejected and retried, then falls back to a mechanical
  rendering. Nothing invented survives.
* **The model never gates.** Cards and blueprints are input to later reasoning;
  no card decides which task ships.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stress_stack.cards import (
    FILE_CARD_SCHEMA,
    EvidenceCard,
    Section,
    check_grounding,
    file_card_messages,
)
from stress_stack.coverage_map import CoverageMap
from stress_stack.graph import RepositoryGraph
from stress_stack.openrouter import ModelError, OpenRouterClient
from stress_stack.prompts import BLUEPRINT_SCHEMA, blueprint_messages
from stress_stack.symbols import ParsedFile

MAX_WORKERS = 20


@dataclass
class CardResult:
    path: str
    status: str
    payload: dict[str, Any] = field(default_factory=dict)
    grounding: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    cached: bool = False
    cost: float = 0.0
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "status": self.status,
            "model": self.model,
            "cached": self.cached,
            "cost": round(self.cost, 6),
            "reason": self.reason,
            "grounding": self.grounding,
            "verification_state": (
                "entity_citations_verified_semantics_unverified"
                if self.grounding.get("grounded")
                else "unverified"
            ),
            **self.payload,
        }


def import_waves(graph: RepositoryGraph) -> list[list[str]]:
    """Group files so each wave only depends on earlier ones.

    Files left over after no wave can be formed are in an import cycle; they are
    emitted as a final wave and simply lose dependency context.
    """
    files = [parsed.path for parsed in graph.files]
    dependencies: dict[str, set[str]] = {path: set() for path in files}
    for edge in graph.edges:
        if edge.kind != "imports":
            continue
        source, target = edge.source.split("::")[0], edge.target.split("::")[0]
        if source != target and source in dependencies:
            dependencies[source].add(target)

    waves: list[list[str]] = []
    placed: set[str] = set()
    while True:
        layer = sorted(p for p in files if p not in placed and not (dependencies[p] - placed))
        if not layer:
            break
        waves.append(layer)
        placed |= set(layer)
    remaining = sorted(p for p in files if p not in placed)
    if remaining:
        waves.append(remaining)
    return waves


def by_path_symbols(graph: RepositoryGraph, path: str) -> list:
    for parsed in graph.files:
        if parsed.path == path:
            return parsed.symbols
    return []


def build_file_card(
    graph: RepositoryGraph,
    coverage: CoverageMap | None,
    parsed: ParsedFile,
    *,
    summaries: dict[str, str] | None = None,
) -> EvidenceCard:
    """Assemble the only thing the model sees for this file."""
    contract: list[str] = []
    entity_ids: list[str] = []
    # Public API first: a large module otherwise fills the card with private
    # helpers and the prompt grows until latency dominates.
    ordered = sorted(
        parsed.symbols,
        key=lambda s: (s.kind == "module" and 0 or 1, not s.is_public, s.anchor.line),
    )
    for symbol in ordered:
        if symbol.kind == "module":
            if symbol.docstring:
                contract.append(f"module: {symbol.docstring.splitlines()[0][:160]}")
            continue
        entity_ids.append(symbol.id)
        # The id must be unambiguously delimited. Gluing the signature straight
        # onto it makes the model copy `id(args)` verbatim — correctly, since
        # that is what the card showed — and every citation then fails to
        # resolve.
        line = f"- {symbol.id} | {symbol.signature or '-'}"
        if symbol.docstring:
            line += f" | {symbol.docstring.splitlines()[0][:110]}"
        contract.append(line)

    behaviour: list[str] = []
    if coverage is not None:
        for symbol_id in entity_ids:
            tests = coverage.covering(symbol_id)
            if tests:
                shown = ", ".join(t.split("::")[-1] for t in tests[:4])
                behaviour.append(f"- {symbol_id.split('::')[-1]} exercised by: {shown}")

    context: list[str] = []
    for reference in parsed.imports:
        target = reference.module or "."
        summary = (summaries or {}).get(target)
        entry = f"- imports {target}"
        if summary:
            entry += f" — {summary[:110]}"
        context.append(entry)

    # A package root usually defines nothing and re-exports everything, so
    # "no defs" would skip the file that states the public API. Describe it by
    # the names it actually re-exports — not every public symbol in the files
    # it imports from.
    if not entity_ids:
        index = {s.qualified_name: s for f in graph.files for s in f.symbols}
        for reference in parsed.imports:
            if not reference.name or reference.name == "*":
                continue
            candidate = index.get(f"{reference.module}.{reference.name}")
            if candidate is None or candidate.kind == "module":
                continue
            entity_ids.append(candidate.id)
            contract.append(
                f"- {candidate.id} | {candidate.signature or '-'} | re-exported as "
                f"{reference.bound_name}"
            )
        contract = contract[:45]
        entity_ids = entity_ids[:45]

    # Still nothing? Visit it anyway. "Defines no functions" is a poor proxy for
    # "unimportant": conftest.py carries shared fixtures and __main__.py proves
    # the package is runnable. Describe what is there and let the blueprint
    # decide whether it is incidental.
    if not contract:
        contract.append(f"- file defines no classes or functions ({len(parsed.imports)} imports)")
        for reference in parsed.imports[:12]:
            contract.append(f"- imports {reference.module or '.'}")

    neighbours = sorted(
        {
            f"- called by {edge.source.split('::')[-1]} ({edge.anchor})"
            for edge in graph.edges
            if edge.kind == "calls"
            and edge.target.startswith(parsed.path + "::")
            and not edge.source.startswith(parsed.path + "::")
        }
    )

    return EvidenceCard(
        subject=parsed.path,
        kind="file",
        entity_ids=entity_ids,
        sections=[
            Section("contract", contract, required=True, max_units=45),
            Section("behaviour", behaviour, max_units=14),
            Section("context", context, max_units=10),
            Section("neighbours", neighbours, max_units=8),
        ],
    )


def describe_file(
    client: OpenRouterClient, card: EvidenceCard, *, role: str = "worker"
) -> CardResult:
    """One model call, grounded, with a single retry on ungrounded output."""
    messages = file_card_messages(card)
    for attempt in (1, 2):
        try:
            payload, completion = client.complete_json(
                messages, schema=FILE_CARD_SCHEMA, role=role, max_tokens=4000,
                use_cache=attempt == 1,
            )
        except ModelError as exc:
            if attempt == 2:
                return CardResult(card.subject, "failed", reason=str(exc)[:200])
            continue
        grounding = check_grounding(payload, card)
        if grounding["grounded"] or attempt == 2:
            return CardResult(
                path=card.subject,
                status="described" if grounding["grounded"] else "ungrounded",
                payload=payload,
                grounding=grounding,
                model=completion.model,
                cached=completion.cached,
                cost=completion.cost,
            )
    return CardResult(card.subject, "failed", reason="unreachable")


def enrich_repository(
    graph: RepositoryGraph,
    coverage: CoverageMap | None,
    client: OpenRouterClient,
    *,
    max_workers: int = MAX_WORKERS,
    role: str = "worker",
) -> list[CardResult]:
    """Describe every file, wave by wave, with a bounded pool inside each wave."""
    by_path = {parsed.path: parsed for parsed in graph.files}
    module_of = {parsed.path: parsed.module for parsed in graph.files}
    summaries: dict[str, str] = {}
    results: list[CardResult] = []

    for wave in import_waves(graph):
        cards = [
            build_file_card(graph, coverage, by_path[path], summaries=summaries)
            for path in wave
            if path in by_path
        ]
        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(cards)))) as pool:
            wave_results = list(pool.map(lambda c: describe_file(client, c, role=role), cards))
        for result in wave_results:
            results.append(result)
            purpose = result.payload.get("purpose")
            module = module_of.get(result.path)
            if purpose and module:
                summaries[module] = str(purpose)
    return results


def synthesize_blueprint(
    client: OpenRouterClient,
    cards: list[CardResult],
    *,
    readme: str = "",
    role: str = "synthesis",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Turn file cards into project intent plus a named feature list."""
    described = [c for c in cards if c.status == "described" and c.payload.get("purpose")]
    if not described:
        return {}, {"status": "skipped", "reason": "no_described_files"}

    lines = [
        f"- {c.path}: {c.payload['purpose']}"
        + (
            "\n    " + "; ".join(str(r) for r in (c.payload.get("responsibilities") or [])[:3])
            if c.payload.get("responsibilities")
            else ""
        )
        for c in described
    ]
    known_paths = {c.path for c in described}
    payload, completion = client.complete_json(
        blueprint_messages(lines, readme=readme),
        schema=BLUEPRINT_SCHEMA,
        role=role,
        max_tokens=6000,
    )
    cited = [
        str(path)
        for feature in payload.get("features", [])
        if isinstance(feature, dict)
        for path in (feature.get("files") or [])
    ]
    unknown = sorted({p for p in cited if p not in known_paths})
    return payload, {
        "status": "grounded" if not unknown and cited else "ungrounded",
        "model": completion.model,
        "cached": completion.cached,
        "cost": round(completion.cost, 6),
        "files_cited": len(set(cited)),
        "unknown_files": unknown,
        "verification_state": "file_citations_verified_semantics_unverified",
    }


def summarize(results: list[CardResult]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    grounded = [r for r in results if r.grounding.get("grounded")]
    return {
        "files": len(results),
        "by_status": dict(sorted(counts.items())),
        "grounded": len(grounded),
        "mean_entity_coverage": (
            round(sum(r.grounding.get("coverage", 0.0) for r in grounded) / len(grounded), 3)
            if grounded
            else 0.0
        ),
        "cost_usd": round(sum(r.cost for r in results), 6),
    }


def write_cards(path: Path, results: list[CardResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(
        json.dumps(result.to_dict(), sort_keys=True, ensure_ascii=False) + "\n"
        for result in sorted(results, key=lambda r: r.path)
    )
    path.write_text(payload, encoding="utf-8")
