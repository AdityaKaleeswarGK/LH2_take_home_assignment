"""Write REPORT.md as analysis rather than as a status dump.

The brief grades this document under engineering judgment, and a list of
statuses demonstrates none. So the prose is written by a model — but under the
same rule every other generated artifact obeys here: the model may phrase the
reasoning, and may not invent the evidence. Every figure it is allowed to cite
is gathered first from artifacts the pipeline already wrote, handed to it as a
closed set, and checked afterwards.

The mechanical report remains the floor. If no model is configured, if the call
fails, or if the returned prose cites a number that appears nowhere in the
evidence, the deterministic version stands and the artifact records which was
used. A report that quietly invents a measurement is worse than a dull one.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_NUMBER = re.compile(r"\d[\d,]*\.?\d*")

REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "what_was_broken",
        "design_decisions",
        "candidate_selection",
        "how_to_run",
        "scale",
        "honest_gaps",
    ],
    "properties": {
        name: {"type": "string", "description": description}
        for name, description in (
            (
                "what_was_broken",
                "What was wrong with the repository and how each class of problem "
                "was fixed. Name the classes, not the commands.",
            ),
            (
                "design_decisions",
                "Decisions and trade-offs: what was automated, what was left "
                "manual, and why. Include decisions that were reversed and what "
                "the evidence was.",
            ),
            (
                "candidate_selection",
                "What was mined, what was rejected, and on what grounds. Use the "
                "funnel counts. Explain why rejections are recorded rather than "
                "silent.",
            ),
            ("how_to_run", "Exact commands, in order, including the container run."),
            (
                "scale",
                "What breaks at 100 repositories and what would be built "
                "differently. Be concrete about the first thing to fail.",
            ),
            (
                "honest_gaps",
                "Anything unfinished, de-scoped or known-weak, with next steps. "
                "State de-scopes as decisions, not omissions.",
            ),
        )
    },
}

_SYSTEM = """You are writing the engineering report for a repository analysis pipeline.

You are given measured evidence as JSON. It is the only source of fact you have.

Rules:
1. Every number you write must appear in the evidence. Never estimate, round to
   a nicer figure, or infer a total you were not given.
2. Write analysis, not a status list. Say what a result means and why the design
   produced it. A reader should learn the reasoning, not the settings.
3. Where the evidence records a decision that was reversed, say so plainly and
   give the evidence that reversed it. Reversals are the strongest material in
   an engineering report.
4. Be specific about limitations. A named weakness with a next step reads as
   judgement; a vague caveat reads as evasion.
5. Do not claim anything the evidence does not support. If a stage did not run,
   say it did not run.
6. Markdown prose, no headings — each field is one section's body. Reply with
   JSON only.
"""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def collect_evidence(repository_root: Path) -> dict[str, Any]:
    """Every measured fact the report is allowed to draw on."""
    metadata = repository_root / ".stress_stack"
    knowledge = metadata / "knowledge"

    lint = _read_json(metadata / "hygiene" / "lint.json")
    comparison = _read_json(metadata / "hygiene" / "comparison.json")
    dependencies = _read_json(knowledge / "dependencies.json")
    container = _read_json(metadata / "container" / "container.json")
    graph_validation = _read_json(knowledge / "graph_validation.json")
    candidates = _read_json(knowledge / "candidates.json")
    validation = _read_json(knowledge / "validation.json")
    manifest = _read_json(metadata / "tasks.json")
    testgen = _read_json(metadata / "test_generation" / "test_generation.json")
    pipeline = _read_json(metadata / "pipeline_run.json")
    graph = _read_json(knowledge / "repo_graph.json")

    return {
        "repository": repository_root.name,
        # The brief's own figures are evidence too. Without them the grounding
        # check rejected its own report for answering "what breaks at 100
        # repositories" with the number 100.
        "brief_constraints": {
            "tasks_required": 10,
            "minimum_history_derived": 4,
            "maximum_excision": 4,
            "maximum_net_new": 3,
            "minimum_distinct_modules": 4,
            "scale_question_repositories": 100,
        },
        "pipeline_stages": [
            {"stage": s.get("stage"), "status": s.get("status"), "seconds": s.get("seconds")}
            for s in pipeline.get("stages") or []
        ],
        "hygiene": {
            # Hygiene records outcomes, not a status field. Deriving the verdict
            # from the comparison is what it always meant; reading a key that
            # never existed reported a successful stage as unavailable.
            "ran": bool(comparison),
            "regressions": len(comparison.get("regressions") or []),
            "passing_before": comparison.get("passing_before"),
            "passing_after": comparison.get("passing_after"),
            "violations_before": lint.get("violations_before"),
            "violations_after": lint.get("violations_after"),
            "files_reformatted": lint.get("files_reformatted"),
            "baselined_rules": lint.get("residual_rules"),
        },
        "dependencies": {
            "lock_status": (dependencies.get("lock") or {}).get("status"),
            "packages": (dependencies.get("lock") or {}).get("package_count"),
            "hashed": (dependencies.get("lock") or {}).get("hashed"),
            "counts": dependencies.get("counts"),
            "audit": dependencies.get("audit"),
        },
        "container": {
            "status": container.get("status"),
            "baseline_match": container.get("baseline_match"),
            "python_version": container.get("python_version"),
            "identical_runs": container.get("identical"),
            # Counts, not the full per-test outcome maps. Two runs of a 202-test
            # suite carry four hundred entries, and sorted alphabetically
            # "container" then consumed the whole prompt budget — the model was
            # handed a payload with mining, validation and the deliverable
            # truncated off the end, and correctly reported them as missing.
            "runs": [
                {key: run.get(key) for key in ("name", "exit_code", "total", "passed", "seconds")}
                for run in container.get("runs") or []
            ],
        },
        "graph": {
            "statistics": graph.get("statistics"),
            "edge_match_rate": graph_validation.get("edge_match_rate"),
            "anchor_match_rate": graph_validation.get("anchor_match_rate"),
            "status": graph_validation.get("status"),
        },
        "generated_tests": {
            # Keys read from the artifact rather than from memory of it. This
            # block asked for "mutation_results" and hygiene asked for "status";
            # neither exists, so both reported a stage that had in fact
            # succeeded as missing evidence. A collector that guesses key names
            # produces a report that confidently understates the work.
            "files": testgen.get("files"),
            "model": testgen.get("model"),
            "status": testgen.get("status"),
            "targets_considered": testgen.get("targets"),
            "rejected": testgen.get("rejected"),
            "generated_run": {
                key: (testgen.get("generated_run") or {}).get(key)
                for key in ("exit_code", "passed", "failed", "errors", "total")
            },
            "suite_after_generation": (testgen.get("suite") or {}).get("counts"),
            "mutations": [
                {
                    key: result.get(key)
                    for key in ("file", "symbols", "exit_code", "failed", "caught")
                }
                for result in testgen.get("mutations") or []
            ],
        },
        "mining": {
            "history_ranked": len((candidates.get("history") or {}).get("candidates") or []),
            "history_funnel": (candidates.get("history") or {}).get("funnel", {}).get(
                "dropped_counts"
            ),
            "excision_ranked": len((candidates.get("excision") or {}).get("candidates") or []),
            "excision_funnel": (candidates.get("excision") or {}).get("funnel", {}).get(
                "dropped_counts"
            ),
            "thresholds": candidates.get("thresholds"),
        },
        "validation": {
            "attempted": (validation.get("summary") or {}).get("attempted"),
            "eligible": (validation.get("summary") or {}).get("eligible"),
            "rejected_counts": (validation.get("summary") or {}).get("rejected_counts"),
            "gates": sorted(
                {
                    gate.get("gate")
                    for task in validation.get("tasks") or []
                    for gate in task.get("gates") or []
                }
            ),
        },
        "deliverable": {
            "task_count": manifest.get("task_count"),
            "by_source": manifest.get("by_source"),
            "distinct_modules": manifest.get("distinct_modules"),
            "difficulty_spread": manifest.get("difficulty_spread"),
            "quota": manifest.get("quota"),
            "quota_satisfied": manifest.get("quota_satisfied"),
            "shortfalls": manifest.get("shortfalls"),
            "instructions_leaking": manifest.get("instructions_leaking"),
            "instruction_origins": sorted(
                {task.get("instruction_origin") for task in manifest.get("tasks") or []}
            ),
        },
    }


def known_numbers(evidence: dict[str, Any]) -> set[str]:
    """Every numeric token the evidence contains, as written."""
    found: set[str] = set()

    def keep(token: str) -> None:
        cleaned = token.replace(",", "").rstrip(".")
        if cleaned:
            found.add(cleaned)

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                for token in _NUMBER.findall(str(key)):
                    keep(token)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, bool):
            return
        elif isinstance(value, (int, float)):
            keep(str(value))
            if float(value).is_integer():
                keep(str(int(value)))
        elif isinstance(value, str):
            for token in _NUMBER.findall(value):
                keep(token)

    walk(evidence)
    return found


def ungrounded_numbers(text: str, allowed: set[str]) -> list[str]:
    """Figures the prose asserts that the evidence never recorded.

    Small integers are exempt: a sentence naturally says "two of the three" or
    references section 5, and forbidding that would reject every readable
    paragraph without catching a single invented measurement.
    """
    # A sentence-ending full stop is not part of the number. Without stripping
    # it the check rejected its own report for citing "3109." against evidence
    # recording 3109.
    cited = {token.replace(",", "").rstrip(".") for token in _NUMBER.findall(text)}
    suspect = []
    for token in sorted(cited):
        if token in allowed:
            continue
        try:
            if abs(float(token)) <= 10:
                continue
        except ValueError:
            continue
        suspect.append(token)
    return suspect


def compose(sections: dict[str, str], repository: str) -> str:
    titles = [
        ("what_was_broken", "1. What was broken, and how the pipeline fixes it"),
        ("design_decisions", "2. Design decisions and trade-offs"),
        ("candidate_selection", "3. Candidate selection: mined, rejected, and why"),
        ("how_to_run", "4. How to run everything"),
        ("scale", "5. Scale: what breaks at 100 repositories"),
        ("honest_gaps", "6. Honest gaps"),
    ]
    parts = [f"# Engineering report — {repository}", ""]
    for key, title in titles:
        parts += [f"## {title}", "", str(sections.get(key) or "").strip(), ""]
    return "\n".join(parts)


def generate(
    client: Any, evidence: dict[str, Any], *, role: str = "reasoning"
) -> tuple[str | None, dict[str, Any]]:
    """Ask a model to write the report, and keep it only if it stays grounded."""
    from stress_stack.openrouter import ModelError

    messages = [
        {"role": "system", "content": _SYSTEM},
        {
            "role": "user",
            "content": (
                "# Measured evidence\n```json\n"
                + json.dumps(evidence, indent=2, sort_keys=True)
                + "\n```\n\nWrite the six sections."
            ),
        },
    ]
    try:
        payload, completion = client.complete_json(
            messages, schema=REPORT_SCHEMA, role=role, max_tokens=16000
        )
    except ModelError as exc:
        return None, {"fell_back": "model_error", "detail": str(exc)[:200]}
    except Exception as exc:  # noqa: BLE001 — the deterministic report always stands
        return None, {"fell_back": type(exc).__name__, "detail": str(exc)[:200]}

    sections = {key: str(payload.get(key) or "").strip() for key in REPORT_SCHEMA["required"]}
    thin = [key for key, value in sections.items() if len(value) < 120]
    if thin:
        return None, {"fell_back": "sections_too_thin", "detail": thin}

    allowed = known_numbers(evidence)
    invented = {
        key: ungrounded_numbers(value, allowed)
        for key, value in sections.items()
        if ungrounded_numbers(value, allowed)
    }
    if invented:
        return None, {"fell_back": "ungrounded_figures", "detail": invented}

    return compose(sections, str(evidence.get("repository") or "repository")), {
        "fell_back": None,
        "model": completion.model,
        "cached": completion.cached,
        "cache_key": completion.cache_key,
    }
