"""Turn validated tasks into the deliverable.

Two steps, deliberately separate. Selection is an audit: it decides the ten and
writes down whether every quota was met, so compliance is a stated fact rather
than something a grader counts folders to establish. Emission is plumbing: it
writes each task's statement and manifest beside the trees validation already
produced.

Nothing here re-decides anything. If selection reports a shortfall, emission
still runs and the shortfall travels into ``tasks.json``, because a deliverable
that quietly looks complete is worse than one that says what it is missing.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from stress_stack.atomic import atomic_write_json
from stress_stack.instruct import (
    build_evidence,
    generated_instruction,
    leak_check,
    mechanical_instruction,
)
from stress_stack.selection import Quota, score_difficulty, select

SCHEMA_VERSION = "0.1.0"


def load_eligible(validation_path: Path) -> list[dict[str, Any]]:
    if not validation_path.is_file():
        return []
    payload = json.loads(validation_path.read_text(encoding="utf-8"))
    return [task for task in payload.get("tasks") or [] if task.get("eligible")]


def lookalike_counts(graph_symbols: list[str], tasks: list[dict[str, Any]]) -> dict[str, int]:
    """How many similarly named symbols exist elsewhere in the repository.

    This is the brief's "misleading similar code" factor, measured: a task whose
    target shares its bare name with several other definitions requires
    identifying the right one rather than searching for it.
    """
    bare: dict[str, int] = {}
    for symbol_id in graph_symbols:
        name = symbol_id.rpartition(".")[2] or symbol_id
        bare[name] = bare.get(name, 0) + 1

    counts: dict[str, int] = {}
    for task in tasks:
        subject = str(task.get("subject") or "")
        name = subject.rpartition(".")[2] or subject
        counts[task["task_id"]] = max(0, bare.get(name, 1) - 1)
    return counts


def pull_request_bodies(history_root: Path) -> dict[int, str]:
    """Pull request descriptions, keyed by number.

    Mining records the body's *length* as a difficulty signal but not its text,
    since nothing in mining needs prose. The instruction writer does, and the
    ingested history already holds it — cheaper to read here than to widen the
    candidate record and re-validate.
    """
    path = history_root / "pull_requests.jsonl"
    if not path.is_file():
        return {}
    bodies: dict[int, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record.get("number"), int):
            bodies[record["number"]] = str(record.get("body") or "")
    return bodies


def base_source_text(input_dir: Path, *, limit: int = 4_000_000) -> str:
    """The pre-change tree as one blob, for the copied-span check to exclude."""
    chunks: list[str] = []
    total = 0
    for path in sorted(input_dir.rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        chunks.append(text)
        total += len(text)
        if total > limit:
            break
    return "\n".join(chunks)


def base_identifiers(input_dir: Path) -> set[str]:
    """Every name defined in the pre-change tree.

    The leak check needs to know which identifiers the change *invented*, and
    that is only answerable against the tree as it stood before.
    """
    names: set[str] = set()
    for path in input_dir.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
    return names


def module_purposes(graph: Any) -> dict[str, str]:
    """One line per module, taken from its own docstring.

    This is where the generated blueprint would supply a richer sentence. Until
    it is wired in, the module's own first docstring line is measured, present
    in every repository that documents itself, and costs nothing.
    """
    purposes: dict[str, str] = {}
    for parsed in graph.files:
        for symbol in parsed.symbols:
            if symbol.kind == "module" and symbol.docstring:
                purposes[parsed.module] = symbol.docstring
                break
    return purposes


def contract_of(graph: Any, symbol_id: str) -> dict[str, str]:
    for parsed in graph.files:
        for symbol in parsed.symbols:
            if symbol.id == symbol_id:
                return {
                    "signature": symbol.signature or "",
                    "docstring": symbol.docstring or "",
                    "qualified_name": symbol.qualified_name,
                }
    return {"signature": "", "docstring": "", "qualified_name": ""}


def run_selection(
    eligible: list[dict[str, Any]], graph: Any, *, quota: Quota | None = None
) -> dict[str, Any]:
    """Select the ten and score their difficulty, together."""
    symbol_ids = [symbol.id for parsed in graph.files for symbol in parsed.symbols]
    ledger, report = select(eligible, quota=quota)
    chosen_ids = {entry["task_id"] for entry in ledger.entries}
    chosen = [task for task in eligible if task["task_id"] in chosen_ids]
    difficulty = score_difficulty(chosen, lookalike_counts(symbol_ids, chosen))

    return {
        "schema_version": SCHEMA_VERSION,
        "pool_size": len(eligible),
        "ledger": ledger.to_dict(),
        "selection": report,
        "difficulty": difficulty,
        "task_ids": [entry["task_id"] for entry in ledger.entries],
    }


def emit_bundle(
    selection: dict[str, Any],
    eligible: list[dict[str, Any]],
    graph: Any,
    tasks_root: Path,
    manifest_path: Path,
    history_root: Path | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """Write each task's statement and manifest, then the top-level manifest."""
    by_id = {task["task_id"]: task for task in eligible}
    purposes = module_purposes(graph)
    bodies = pull_request_bodies(history_root) if history_root else {}
    difficulty = selection["difficulty"]
    entries: list[dict[str, Any]] = []
    leaks: list[str] = []

    for task_id in selection["task_ids"]:
        task = by_id[task_id]
        task_root = tasks_root / task_id
        contract = (
            contract_of(graph, str(task.get("subject") or ""))
            if task["source"] == "excision"
            else {"signature": "", "docstring": "", "qualified_name": ""}
        )
        evidence = build_evidence(
            task,
            module_purpose=purposes.get(task.get("primary_module") or "", ""),
            signature=contract["signature"],
            docstring=contract["docstring"],
            qualified_name=contract["qualified_name"],
            pr_body=bodies.get(int((task.get("signals") or {}).get("pr_number") or 0), ""),
        )
        written = mechanical_instruction(task, evidence)
        origin = "mechanical"
        generated_meta: dict[str, Any] = {}

        diff_path = task_root / "goldenSolution.diff"
        diff = diff_path.read_text(encoding="utf-8") if diff_path.is_file() else ""
        input_dir = task_root / "input"
        names = base_identifiers(input_dir)
        text = base_source_text(input_dir)
        report = leak_check(written["instruction"], diff, names, text)

        # The template is the floor. Generated prose has to clear the same leak
        # check to replace it, and anything else — a model error, a short or
        # unusable answer, a leak — leaves the template standing.
        if client is not None and report.clean:
            produced = generated_instruction(
                client,
                task,
                evidence,
                diff=diff,
                base_names=names,
                base_text=text,
                written_so_far="\n".join(f"- {entry['title']}" for entry in entries[-4:]),
            )
            if produced is not None:
                written, generated_meta = produced
                origin = "generated"
                report = leak_check(written["instruction"], diff, names, text)

        if not report.clean:
            leaks.append(task_id)

        record = {
            "id": task_id,
            "source": task["source"],
            "title": written["title"],
            "instruction": written["instruction"],
            "instruction_origin": origin,
            "generation": generated_meta,
            "leak_check": report.to_dict(),
            "difficulty": difficulty.get(task_id, {}),
            "primary_module": task.get("primary_module"),
            "modules": task.get("modules") or [],
            "files_in_scope": task.get("files_in_scope") or [],
            "verifier": {
                "files": task.get("verifier_files") or [],
                "node_ids": task.get("targets") or [],
                "procedure": (
                    "Copy input/, overlay verifier/ onto it, then run the listed "
                    "node ids. They must fail before the change and pass after."
                ),
            },
            "paths": {
                "input": f"{task_id}/input",
                "solution": f"{task_id}/solution",
                "verifier": f"{task_id}/verifier",
                "golden_diff": f"{task_id}/goldenSolution.diff",
                "evidence": f"{task_id}/evidence",
            },
            "gates": task.get("gates") or [],
        }
        atomic_write_json(task_root / "task.json", record)
        entries.append({key: record[key] for key in record if key != "gates"})

    # A previous selection leaves its statements behind. Validation owns the
    # trees and keeps them all as evidence, but a task.json is a claim that this
    # task ships — and after a re-run the pool can shift, which left glom with
    # thirteen statements on disk beside a manifest listing ten. Stale claims
    # are removed so the directory and the manifest cannot disagree.
    shipped = set(selection["task_ids"])
    withdrawn: list[str] = []
    if tasks_root.is_dir():
        for candidate in sorted(tasks_root.iterdir()):
            statement = candidate / "task.json"
            if candidate.is_dir() and statement.is_file() and candidate.name not in shipped:
                statement.unlink()
                withdrawn.append(candidate.name)

    ledger = selection["ledger"]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "task_count": len(entries),
        "by_source": ledger["by_source"],
        "by_primary_module": ledger["by_primary_module"],
        "modules_covered": ledger["modules_covered"],
        "distinct_modules": ledger["distinct_modules"],
        "quota": ledger["quota"],
        "quota_satisfied": ledger["satisfied"],
        "shortfalls": ledger["shortfalls"],
        "difficulty_spread": _spread(difficulty),
        "instructions_leaking": leaks,
        "pool_size": selection["pool_size"],
        "validated_not_shipped": sorted(
            task["task_id"] for task in eligible if task["task_id"] not in shipped
        ),
        "withdrawn_statements": withdrawn,
        "tasks": entries,
    }
    atomic_write_json(manifest_path, manifest)
    return manifest


def _spread(difficulty: dict[str, dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in difficulty.values():
        tier = str(entry.get("tier") or "unknown")
        counts[tier] = counts.get(tier, 0) + 1
    return dict(sorted(counts.items()))
