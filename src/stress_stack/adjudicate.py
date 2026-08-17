"""Decide how hard each selected task is, by looking at the code.

The brief asks for a difficulty label *and* a justification, and says plainly
what it is grading: "we are grading the reasoning, not a measured score." A
tercile cut over five normalised factors answers a different question — it
ranks, which is not the same as explaining — and the prose it produced was
assembled from factor names, with the seam showing.

So this stage gives the judgement to an agent that can open the repository. It
reads the module, follows the callers, looks for the near-identical helper an
agent would edit by mistake, and then says which of the brief's criteria the
task actually meets. Two passes, because easy/medium/hard is only meaningful
across a set: each task is judged alone, then one call sees all ten together and
settles the labels.

Nothing here gates. Difficulty is a label; no gate, no quota, and no selection
decision reads it. The measured tier is kept beside the chosen one so the two can
disagree in the open, which is the only honest way to ship a judgement that
replaced a measurement.

If no model is configured the measured tier stands and the stage says so.
"""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stress_stack.explore import TOOLS, Explorer
from stress_stack.openrouter import ModelError, OpenRouterClient
from stress_stack.prompts import (
    ADJUDICATION_SCHEMA,
    CALIBRATION_SCHEMA,
    adjudication_messages,
    calibration_messages,
)

TIERS = ("easy", "medium", "hard")
MEASURED = "measured_tercile"
ADJUDICATED = "agent"


@dataclass
class Verdict:
    """One task's difficulty, and everything behind it."""

    task_id: str
    tier: str
    justification: str
    origin: str
    measured_tier: str = ""
    criteria: list[str] = field(default_factory=list)
    explored: str = ""
    exploration: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    @property
    def agrees_with_measurement(self) -> bool:
        return self.tier == self.measured_tier

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "tier": self.tier,
            "justification": self.justification,
            "origin": self.origin,
            "measured_tier": self.measured_tier,
            "agrees_with_measurement": self.agrees_with_measurement,
            "criteria": self.criteria,
            "explored": self.explored,
            "exploration": self.exploration,
            "usage": self.usage,
            "note": self.note,
            "verification_state": (
                "reasoning_unverified_label_only"
                if self.origin == ADJUDICATED
                else "measured"
            ),
        }


def adjudicate(
    tasks: list[dict[str, Any]],
    difficulty: dict[str, dict[str, Any]],
    *,
    tasks_root: Path,
    client: OpenRouterClient | None,
    index_path: Path | None = None,
    blueprint: dict[str, Any] | None = None,
    # Measured on glom: the verdicts that needed twelve turns were no better
    # than the ones that took three. pr-262 reached a well-argued "easy" in four
    # tool calls and pr-277 a "hard" in five, while the twelve-turn tasks spent
    # the budget re-reading. Eight leaves room to follow a caller chain and stops
    # the tail from setting the wall clock for the whole stage.
    max_turns: int = 8,
    max_workers: int = 10,
) -> dict[str, Any]:
    """Judge every selected task, then calibrate the labels across the set."""
    fallback = {
        task["task_id"]: Verdict(
            task_id=task["task_id"],
            tier=str((difficulty.get(task["task_id"]) or {}).get("tier") or "medium"),
            justification=str(
                (difficulty.get(task["task_id"]) or {}).get("justification") or ""
            ),
            origin=MEASURED,
            measured_tier=str((difficulty.get(task["task_id"]) or {}).get("tier") or ""),
        )
        for task in tasks
    }

    if client is None or not client.configured:
        return _result(
            list(fallback.values()), calibrated=False, note="no_model_configured"
        )

    def judge(task: dict[str, Any]) -> Verdict:
        return _judge_one(
            task,
            difficulty.get(task["task_id"]) or {},
            tasks_root=tasks_root,
            client=client,
            index_path=index_path,
            blueprint=blueprint or {},
            max_turns=max_turns,
            fallback=fallback[task["task_id"]],
        )

    # Each task is judged independently and each opens its own index handle, so
    # the pool is safe and the wall clock is one task rather than ten.
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(tasks)))) as pool:
        verdicts = list(pool.map(judge, tasks))

    calibrated, note = _calibrate(verdicts, tasks, client)
    return _result(calibrated, calibrated=not note, note=note)


def _judge_one(
    task: dict[str, Any],
    measured: dict[str, Any],
    *,
    tasks_root: Path,
    client: OpenRouterClient,
    index_path: Path | None,
    blueprint: dict[str, Any],
    max_turns: int,
    fallback: Verdict,
) -> Verdict:
    """Explore, then answer. A model failure leaves the measured tier standing.

    The index handle is opened here rather than shared, because a ``sqlite3``
    connection belongs to the thread that created it and these run in a pool.
    """
    task_id = task["task_id"]
    index = _open_index(index_path)
    explorer = Explorer(tree=tasks_root / task_id / "input", index=index)
    brief = task_brief(task, measured, blueprint)
    try:
        return _explore_and_answer(
            task_id, measured, client, explorer, brief, max_turns, fallback
        )
    finally:
        if index is not None:
            index.close()


def _explore_and_answer(
    task_id: str,
    measured: dict[str, Any],
    client: OpenRouterClient,
    explorer: Explorer,
    brief: str,
    max_turns: int,
    fallback: Verdict,
) -> Verdict:
    try:
        conversation, completions = client.converse(
            adjudication_messages(brief),
            tools=TOOLS,
            run_tool=explorer.run,
            max_turns=max_turns,
            role="worker",
            max_tokens=8000,
        )
        # The verdict is asked for separately, with the schema attached and the
        # tools withdrawn. Providers differ on whether structured output and tool
        # use may be requested together, and a turn that is allowed to call a
        # tool instead of answering can decline to finish.
        payload, completion = client.complete_json(
            [
                *conversation,
                {
                    "role": "user",
                    "content": "Give your verdict now. Return JSON with keys: "
                    "explored, criteria, tier, justification.",
                },
            ],
            schema=ADJUDICATION_SCHEMA,
            role="worker",
            max_tokens=4000,
        )
    except (ModelError, Exception) as exc:  # noqa: BLE001 — a label never fails a run
        fallback.note = f"{type(exc).__name__}: {str(exc)[:200]}"
        fallback.exploration = explorer.to_dict()
        return fallback

    tier = str(payload.get("tier") or "").strip().lower()
    justification = str(payload.get("justification") or "").strip()
    if tier not in TIERS or not justification:
        fallback.note = f"unusable_verdict: tier={tier!r}"
        fallback.exploration = explorer.to_dict()
        return fallback

    return Verdict(
        task_id=task_id,
        tier=tier,
        justification=justification,
        origin=ADJUDICATED,
        measured_tier=str(measured.get("tier") or ""),
        criteria=[str(item) for item in (payload.get("criteria") or [])],
        explored=str(payload.get("explored") or "")[:2000],
        exploration=explorer.to_dict(),
        usage={
            "turns": len(completions),
            "model": completion.model,
            "prompt_hash": completion.cache_key,
            "cached": completion.cached and all(c.cached for c in completions),
        },
    )


def _calibrate(
    verdicts: list[Verdict], tasks: list[dict[str, Any]], client: OpenRouterClient
) -> tuple[list[Verdict], str]:
    """One pass over the whole set. Returns the verdicts and why, if it failed."""
    judged = [v for v in verdicts if v.origin == ADJUDICATED]
    if len(judged) < 2:
        return verdicts, "too_few_agent_verdicts_to_calibrate"

    by_id = {task["task_id"]: task for task in tasks}
    proposals = [
        {
            "task_id": v.task_id,
            "title": by_id.get(v.task_id, {}).get("title", ""),
            "source": by_id.get(v.task_id, {}).get("source", ""),
            "modules": by_id.get(v.task_id, {}).get("modules") or [],
            "tier": v.tier,
            "criteria": v.criteria,
            "measured_tier": v.measured_tier,
            "justification": v.justification,
        }
        for v in judged
    ]
    try:
        payload, _ = client.complete_json(
            calibration_messages(proposals),
            schema=CALIBRATION_SCHEMA,
            # Comparing ten judgements against each other is the reasoning role,
            # not the worker one. There is no "reviewer" role configured, and
            # naming one silently degraded every calibration to a failed call.
            role="reasoning",
            max_tokens=6000,
        )
    except Exception as exc:  # noqa: BLE001 — keep the per-task verdicts
        return verdicts, f"calibration_failed: {type(exc).__name__}: {str(exc)[:160]}"

    settled = {
        str(entry.get("task_id")): entry
        for entry in (payload.get("tiers") or [])
        if isinstance(entry, dict)
    }
    missing = {v.task_id for v in judged} - set(settled)
    if missing:
        # A partial answer would relabel some tasks against the set and leave
        # others judged alone, which is a worse basis than either pass alone.
        return verdicts, f"calibration_incomplete: {len(missing)} task(s) unreturned"

    for verdict in judged:
        entry = settled[verdict.task_id]
        tier = str(entry.get("tier") or "").strip().lower()
        justification = str(entry.get("justification") or "").strip()
        if tier not in TIERS or not justification:
            continue
        if tier != verdict.tier:
            verdict.note = f"calibrated_from_{verdict.tier}"
        verdict.tier = tier
        verdict.justification = justification
    return verdicts, ""


def task_brief(
    task: dict[str, Any], measured: dict[str, Any], blueprint: dict[str, Any]
) -> str:
    """What the agent is told before it starts looking.

    The measurements are offered as evidence, not as an answer to ratify. They
    are named as counts rather than as a tier so the number does not anchor the
    label the model is being asked to choose.
    """
    signals = task.get("signals") or {}
    factors = measured.get("factors") or {}
    lines = [
        "# Task under review",
        f"- id: {task.get('task_id')}",
        f"- title: {task.get('title')}",
        f"- source: {task.get('source')} "
        f"({'a real change from the repository history' if task.get('source') == 'history' else 'a function body removed from the current tree'})",
        f"- primary module: {task.get('primary_module')}",
        f"- modules touched: {', '.join(task.get('modules') or []) or '(none recorded)'}",
        "",
        "## Files the change is scoped to",
        *_bullets(task.get("files_in_scope"), 20),
        "",
        "## Tests that decide pass or fail",
        *_bullets(task.get("targets"), 15),
        "",
        "## Measurements already taken",
        f"- source files changed: {int(factors.get('coordinated_change') or 0)}",
        f"- modules spanned: {int(factors.get('cross_module') or 0)}",
        f"- similarly-named symbols elsewhere in the repository: "
        f"{int(factors.get('misleading_similarity') or 0)}",
        f"- lines changed: {int(signals.get('churn') or signals.get('body_lines') or 0)}",
    ]

    feature = _feature_for(task, blueprint)
    if feature:
        lines += [
            "",
            "## What this area of the repository does",
            f"- {feature.get('name')}: {str(feature.get('definition') or '')[:600]}",
        ]

    lines += [
        "",
        "The pre-change tree is what the agent will start from, and it is what "
        "your tools read. The fix itself is not available to you, deliberately — "
        "you are judging the difficulty of the work, not reviewing a diff.",
    ]
    return "\n".join(lines)


def _bullets(values: Any, limit: int) -> list[str]:
    items = [f"- {value}" for value in (values or [])[:limit]]
    return items or ["- (none recorded)"]


def _feature_for(
    task: dict[str, Any], blueprint: dict[str, Any]
) -> dict[str, Any] | None:
    """The blueprint feature whose files overlap this task's scope, if any."""
    scope = {str(path) for path in (task.get("files_in_scope") or [])}
    if not scope:
        return None
    best, overlap = None, 0
    for feature in blueprint.get("features") or []:
        if not isinstance(feature, dict):
            continue
        shared = len(scope & {str(path) for path in (feature.get("files") or [])})
        if shared > overlap:
            best, overlap = feature, shared
    return best


def _open_index(path: Path | None) -> sqlite3.Connection | None:
    if path is None or not path.is_file():
        return None
    try:
        from stress_stack.index import connect

        return connect(path)
    except sqlite3.Error:
        return None


def _result(verdicts: list[Verdict], *, calibrated: bool, note: str) -> dict[str, Any]:
    ordered = sorted(verdicts, key=lambda v: v.task_id)
    agreed = sum(1 for v in ordered if v.origin == ADJUDICATED and v.agrees_with_measurement)
    judged = sum(1 for v in ordered if v.origin == ADJUDICATED)
    return {
        "schema_version": "0.1.0",
        "calibrated": calibrated,
        "note": note,
        "adjudicated": judged,
        "fell_back": len(ordered) - judged,
        "agreed_with_measurement": agreed,
        "tiers": {v.task_id: v.tier for v in ordered},
        "distribution": {
            tier: sum(1 for v in ordered if v.tier == tier) for tier in TIERS
        },
        "verdicts": [v.to_dict() for v in ordered],
    }


def load_adjudication(path: Path) -> dict[str, dict[str, Any]]:
    """Re-read a written adjudication, keyed by task id. Absent is not an error."""
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        str(entry.get("task_id")): entry
        for entry in (payload.get("verdicts") or [])
        if isinstance(entry, dict) and entry.get("task_id")
    }
