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
import re
import statistics
from pathlib import Path
from typing import Any

from stress_stack.atomic import atomic_write_json
from stress_stack.instruct import (
    build_evidence,
    generated_instruction,
    instruction_quality,
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


def module_purposes(graph: Any, blueprint_path: Path | None = None) -> dict[str, str]:
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
    # Pipeline 2 is machine input to Pipeline 3: a grounded feature definition
    # replaces the raw module-docstring fallback for every file it cites.
    if blueprint_path and blueprint_path.is_file():
        try:
            blueprint = json.loads(blueprint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            blueprint = {}
        if (blueprint.get("_meta") or {}).get("status") == "grounded":
            module_by_path = {parsed.path: parsed.module for parsed in graph.files}
            for feature in blueprint.get("features") or []:
                if not isinstance(feature, dict):
                    continue
                definition = str(feature.get("definition") or "").strip()
                if not definition:
                    continue
                for path in feature.get("files") or []:
                    module = module_by_path.get(str(path))
                    if module:
                        purposes[module] = definition
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


def provenance_of(task: dict[str, Any]) -> dict[str, Any]:
    """Where this task came from, in the terms the brief asks for.

    A history task is provenanced by the commit pair it was materialised
    between; an excision task by the symbol whose body was removed. Both carry
    enough to re-derive the task from the repository without this tool.
    """
    signals = task.get("signals") or {}
    transition = (task.get("detail") or {}).get("transition") or {}
    if task.get("source") == "excision":
        return {
            "kind": "excision_target",
            "symbol_id": signals.get("symbol_id"),
            "qualified_name": signals.get("qualified_name"),
            "path": signals.get("path"),
            "commit_sha": transition.get("head_sha"),
            "stub_strategy": ((task.get("detail") or {}).get("excision") or {}).get("strategy"),
        }
    return {
        "kind": "commit",
        "commit_sha": transition.get("head_sha"),
        "base_sha": transition.get("base_sha"),
        "pull_request": signals.get("pr_number"),
        "url": signals.get("html_url"),
        "merged_at": signals.get("merged_at"),
    }


def verifier_command(task: dict[str, Any], tasks_root: Path) -> list[str]:
    """The exact command that decides pass or fail, as an argument vector."""
    from stress_stack.runner import pytest_arguments

    tree = tasks_root / task["task_id"] / "input"
    return [
        "python", "-m", "pytest", "-p", "no:cacheprovider",
        "--continue-on-collection-errors", "-q",
        *pytest_arguments(tree, task.get("targets") or []),
    ]


def validation_status(task: dict[str, Any]) -> dict[str, Any]:
    """What was proved about this task, gate by gate."""
    gates = task.get("gates") or []
    return {
        "status": "validated" if task.get("eligible") else "rejected",
        "gates_passed": [g["gate"] for g in gates if g.get("passed")],
        "gates_failed": [g["gate"] for g in gates if not g.get("passed")],
        "repeats": (task.get("detail") or {}).get("repeats"),
        "fail_before_classes": (task.get("detail") or {}).get("failure_classes", {}),
        "runs": [r["name"] for r in task.get("runs") or []],
    }


def golden_solution_markdown(
    task: dict[str, Any], diff: str, provenance: dict[str, Any]
) -> str:
    """The diff, plus why this is the correct fix.

    The rationale is assembled from measured evidence rather than asserted: the
    change is correct because these named tests do not pass before it and do
    pass after, nothing else regressed, and the verdict repeated.
    """
    detail = task.get("detail") or {}
    targets = task.get("targets") or []
    screen = detail.get("screen") or {}
    lines = [
        f"# Golden solution — {task['task_id']}",
        "",
        "## Provenance",
        "",
    ]
    lines += [f"- **{key}**: `{value}`" for key, value in provenance.items() if value]
    lines += [
        "",
        "## Why this is the correct fix",
        "",
        "This is the change the repository itself made, taken from git rather than "
        "written by hand. It is *verified* correct rather than assumed, by four "
        "measurements recorded in `evidence/`:",
        "",
        f"1. **Fail-before.** {len(targets)} designated test(s) do not pass against "
        f"`input/`. {len(screen.get('ran_and_failed', []))} of them ran and failed "
        "for a behavioural reason — an assertion, or an exception raised from inside "
        "the repository — rather than an import or collection error.",
        f"2. **Pass-after.** The same tests pass against `solution/`.",
        f"3. **No collateral breakage.** Every test passing before the change still "
        "passes after it.",
        f"4. **Determinism.** The verdict was reproduced across "
        f"{detail.get('repeats', 'N')} fresh container runs with identical statuses "
        "and identical failure signatures.",
        "",
        "### Designated tests",
        "",
    ]
    lines += [f"- `{target}`" for target in targets] or ["- (none recorded)"]
    lines += ["", "## Diff", "", "```diff", diff.rstrip(), "```", ""]
    return "\n".join(lines)


def run_selection(
    eligible: list[dict[str, Any]], graph: Any, *, quota: Quota | None = None
) -> dict[str, Any]:
    """Select the ten and score their difficulty, together."""
    symbol_ids = [symbol.id for parsed in graph.files for symbol in parsed.symbols]
    coherent, rejected = _coherent_pool(eligible)
    ledger, report = select(coherent, quota=quota)
    chosen_ids = {entry["task_id"] for entry in ledger.entries}
    chosen = [task for task in coherent if task["task_id"] in chosen_ids]
    difficulty = score_difficulty(chosen, lookalike_counts(symbol_ids, chosen))

    return {
        "schema_version": SCHEMA_VERSION,
        "pool_size": len(eligible),
        "coherent_pool_size": len(coherent),
        "quality_rejected": rejected,
        "ledger": ledger.to_dict(),
        "selection": report,
        "difficulty": difficulty,
        "task_ids": [entry["task_id"] for entry in ledger.entries],
    }


def _coherent_pool(tasks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Remove history snapshots that are repository maintenance, not one task."""
    history = [task for task in tasks if task.get("source") == "history"]
    file_counts = [int((task.get("signals") or {}).get("files_changed") or 0) for task in history]
    churns = [int((task.get("signals") or {}).get("churn") or 0) for task in history]
    median_files = statistics.median(file_counts) if file_counts else 0
    median_churn = statistics.median(churns) if churns else 0
    file_ceiling = max(25, int(median_files * 4))
    churn_ceiling = max(2_000, int(median_churn * 6))
    generic = re.compile(r"^(?:stable|main|master|merge\b|sync\b|release\b)", re.I)

    kept: list[dict[str, Any]] = []
    rejected: dict[str, str] = {}
    for task in tasks:
        if task.get("source") != "history":
            kept.append(task)
            continue
        title = str(task.get("title") or "").strip()
        signals = task.get("signals") or {}
        files = int(signals.get("files_changed") or 0)
        churn = int(signals.get("churn") or 0)
        if generic.search(title):
            rejected[str(task["task_id"])] = "generic_or_merge_title"
        elif files > file_ceiling and churn > churn_ceiling:
            rejected[str(task["task_id"])] = (
                f"change_too_broad:{files}_files:{churn}_lines"
            )
        else:
            kept.append(task)
    return kept, dict(sorted(rejected.items()))


def merge_adjudication(
    measured: dict[str, dict[str, Any]], path: Path | None
) -> dict[str, dict[str, Any]]:
    """Let the agent's tier win, and keep the measurement beside it.

    The two are recorded together on purpose. A judgement that replaced a
    measurement should be auditable against the thing it replaced, and a reader
    who distrusts the reasoning can still see what the factors said. Where no
    adjudication exists — no model, a failed call — the measured tier stands
    untouched and says so.
    """
    from stress_stack.adjudicate import load_adjudication

    verdicts = load_adjudication(path) if path is not None else {}
    merged: dict[str, dict[str, Any]] = {}
    for task_id, entry in measured.items():
        verdict = verdicts.get(task_id)
        record = dict(entry)
        record["measured_tier"] = entry.get("tier")
        record["measured_justification"] = entry.get("justification")
        if not verdict:
            record["tier_origin"] = "measured_tercile"
            merged[task_id] = record
            continue
        record["tier"] = verdict.get("tier") or entry.get("tier")
        record["justification"] = verdict.get("justification") or entry.get("justification")
        record["tier_origin"] = verdict.get("origin") or "measured_tercile"
        record["criteria"] = verdict.get("criteria") or []
        record["agrees_with_measurement"] = verdict.get("agrees_with_measurement")
        record["verification_state"] = verdict.get("verification_state")
        record["exploration"] = {
            key: value
            for key, value in (verdict.get("exploration") or {}).items()
            if key != "calls"
        }
        merged[task_id] = record
    return merged


def emit_bundle(
    selection: dict[str, Any],
    eligible: list[dict[str, Any]],
    graph: Any,
    tasks_root: Path,
    manifest_path: Path,
    history_root: Path | None = None,
    knowledge_root: Path | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """Write each task's statement and manifest, then the top-level manifest."""
    by_id = {task["task_id"]: task for task in eligible}
    purposes = module_purposes(
        graph, knowledge_root / "blueprint.json" if knowledge_root else None
    )
    bodies = pull_request_bodies(history_root) if history_root else {}
    difficulty = merge_adjudication(
        selection["difficulty"],
        knowledge_root / "adjudication.json" if knowledge_root else None,
    )
    entries: list[dict[str, Any]] = []
    leaks: list[str] = []
    invalid_instructions: list[str] = []

    from stress_stack.progress import reporter

    live = reporter()
    for position, task_id in enumerate(selection["task_ids"], start=1):
        live.step(f"writing {task_id}", position, len(selection["task_ids"]))
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

        # A leaking template is the case that most needs a second attempt, so
        # generation is never gated on the template being clean. Gating it that
        # way meant three click tasks shipped a leaking mechanical instruction
        # while the model that could have replaced it was never asked.
        if client is not None:
            produced, generated_meta = generated_instruction(
                client,
                task,
                evidence,
                diff=diff,
                base_names=names,
                base_text=text,
                written_so_far="\n".join(f"- {entry['title']}" for entry in entries[-4:]),
            )
            if produced is not None:
                written = produced
                origin = "generated"
                report = leak_check(written["instruction"], diff, names, text)
            else:
                origin = "mechanical_model_fallback"

        quality_ok, quality_problems = instruction_quality(
            written["title"], written["instruction"]
        )
        if not quality_ok:
            written = mechanical_instruction(task, dict(evidence, change_summary=""))
            origin = "mechanical_quality_fallback"
            generated_meta = {
                **generated_meta,
                "quality_fallback": quality_problems,
            }
            quality_ok, quality_problems = instruction_quality(
                written["title"], written["instruction"]
            )
        if not quality_ok:
            invalid_instructions.append(task_id)

        if not report.clean:
            # Last resort: the change summary is the only part of the template
            # carrying prose the author wrote, and therefore the only part that
            # can echo the patch. Dropping it costs context and guarantees a
            # clean statement, which is the right trade when the alternative is
            # shipping the answer.
            stripped = dict(evidence, change_summary="")
            written = mechanical_instruction(task, stripped)
            origin = "mechanical_minimal"
            generated_meta = {}
            report = leak_check(written["instruction"], diff, names, text)

        if not report.clean:
            leaks.append(task_id)

        # The difficulty justification is prose about the change, written by an
        # agent that read the pre-change tree. It ships in task.json beside the
        # instruction, so it is held to the same standard: if it echoes the
        # patch, the measured sentence — which is assembled from factor names
        # and cannot leak — takes its place.
        entry = difficulty.get(task_id) or {}
        if entry.get("tier_origin") == "agent":
            justification_leak = leak_check(str(entry.get("justification") or ""), diff, names, text)
            if not justification_leak.clean:
                entry = dict(
                    entry,
                    justification=entry.get("measured_justification") or "",
                    tier_origin="agent_tier_measured_justification",
                    justification_leak=justification_leak.to_dict(),
                )
                difficulty[task_id] = entry

        provenance = provenance_of(task)
        command = verifier_command(task, tasks_root)
        (task_root / "goldenSolution.md").write_text(
            golden_solution_markdown(task, diff, provenance), encoding="utf-8"
        )

        # The brief describes verifier/ as "tests + run command that decide
        # pass/fail". Recording the command only in task.json satisfies the
        # letter and not the shape: the directory should be self-contained.
        verifier_root = task_root / "verifier"
        verifier_root.mkdir(parents=True, exist_ok=True)
        (verifier_root / "run.sh").write_text(
            "#!/usr/bin/env bash\n"
            "# Copy input/ elsewhere, lay these files over it, and run this from\n"
            "# the tree root. These ids must fail before the change, pass after.\n"
            "set -euo pipefail\n"
            + " ".join(command)
            + "\n",
            encoding="utf-8",
        )
        (verifier_root / "targets.txt").write_text(
            "\n".join(task.get("targets") or []) + "\n", encoding="utf-8"
        )

        record = {
            "id": task_id,
            "source": task["source"],
            "provenance": provenance,
            "validation": validation_status(task),
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
                "command": command,
                "command_line": " ".join(command),
                "procedure": (
                    "Copy input/, overlay verifier/ onto it, then run the command "
                    "below from the tree root. It must fail before the change and "
                    "pass after."
                ),
            },
            "paths": {
                "input": f"{task_id}/input",
                "solution": f"{task_id}/solution",
                "verifier": f"{task_id}/verifier",
                "golden_diff": f"{task_id}/goldenSolution.diff",
                "golden_solution": f"{task_id}/goldenSolution.md",
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
        "instructions_invalid": invalid_instructions,
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
