"""An agent that clears residual lint violations, and is never believed.

Autofix handles the mechanical violations. What remains is the set a linter can
identify but not rewrite — and those are exactly the ones where a model is
useful and also where it is most likely to change behaviour while "fixing
style".

So every round is a proposal, not an edit: the model's rewrite is applied, the
linter is re-counted, the suite is re-run in the container, and the round is
kept only if violations went *down* and the suite output is *byte-identical* to
before. Anything else is reverted. A round that cannot be verified is discarded
even if it looks correct, because an unverified improvement to a benchmark's
environment is indistinguishable from a subtle break in it.

The loop stops on the first round that fails to improve, so a model that cannot
solve the remaining violations costs one round rather than the full budget.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stress_stack.tooling import run

# Files past this size are not sent: the round-trip cost stops being worth one
# lint violation, and a truncated file would be rewritten from partial context.
_MAX_FILE_BYTES = 60_000

REPAIR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": ["path", "content", "rationale"],
            },
        }
    },
    "required": ["edits"],
}


@dataclass
class RepairRound:
    round_number: int
    violations_before: int
    violations_after: int
    accepted: bool
    reason: str
    files_touched: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round_number,
            "violations_before": self.violations_before,
            "violations_after": self.violations_after,
            "accepted": self.accepted,
            "reason": self.reason,
            "files_touched": self.files_touched,
        }


@dataclass
class RepairResult:
    status: str
    rounds: list[RepairRound] = field(default_factory=list)
    violations_start: int = 0
    violations_end: int = 0
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "violations_start": self.violations_start,
            "violations_end": self.violations_end,
            "violations_cleared": max(0, self.violations_start - self.violations_end),
            "rounds": [entry.to_dict() for entry in self.rounds],
            "reason": self.reason,
        }


def _snapshot_tracked(root: Path) -> str | None:
    """The current tracked-file state, as a diff we can restore from."""
    result = run(["git", "diff"], cwd=root, timeout=300.0)
    return result.stdout if result.ok else None


def _restore(root: Path) -> bool:
    return run(["git", "checkout", "--", "."], cwd=root, timeout=300.0).ok


def _violation_files(root: Path, residual: dict[str, int], language: str) -> list[Path]:
    """Source files plausibly carrying the residual violations."""
    suffixes = {
        "rust": (".rs",),
        "go": (".go",),
        "typescript": (".ts", ".tsx"),
        "javascript": (".js", ".jsx", ".mjs"),
    }.get(language, ())
    if not suffixes:
        return []
    return [
        path
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and path.suffix in suffixes
        and ".stress_stack" not in path.parts
        and not any(part.startswith(".") for part in path.relative_to(root).parts[:-1])
        and path.stat().st_size <= _MAX_FILE_BYTES
    ][:12]


def _ask_for_edits(
    client: Any, language: str, tool: str, violations: str, files: list[tuple[str, str]]
) -> list[dict[str, str]]:
    listing = "\n\n".join(f"=== {path} ===\n{content}" for path, content in files)
    messages = [
        {
            "role": "system",
            "content": (
                "You fix linter violations without changing behaviour. Return the "
                "complete new content for only the files you change. Never alter "
                "control flow, arithmetic, error handling, public signatures, or "
                "test expectations. If a violation cannot be fixed without a "
                "behaviour change, leave that file out of your answer entirely."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Language: {language}\nLinter: {tool}\n\n"
                f"Violations reported:\n{violations}\n\n"
                f"Files:\n{listing}\n\n"
                "Return JSON: {\"edits\": [{\"path\", \"content\", \"rationale\"}]}"
            ),
        },
    ]
    payload, _ = client.complete_json(
        messages, schema=REPAIR_SCHEMA, role="worker", max_tokens=8000
    )
    edits = payload.get("edits")
    return [entry for entry in edits if isinstance(entry, dict)] if isinstance(edits, list) else []


def repair_lint_violations(
    root: Path | str,
    language: str,
    *,
    client: Any | None,
    probe_image: str | None,
    baseline_output: str,
    max_rounds: int = 2,
) -> RepairResult:
    """Try to clear residual violations, keeping only verified improvements."""
    root = Path(root)
    from stress_stack.hygiene_verify import build_probe_image, snapshot_suite
    from stress_stack.linters import lint

    if client is None:
        return RepairResult(status="skipped", reason="no_model_configured")
    if probe_image is None:
        # Without a way to re-run the suite there is no way to tell a fix from a
        # break, and an unverifiable repair is not worth making.
        return RepairResult(status="skipped", reason="no_probe_image_to_verify_against")

    start = lint(root, language)
    if start.status != "linted" or start.violations_after == 0:
        return RepairResult(
            status="not_needed",
            violations_start=start.violations_after,
            violations_end=start.violations_after,
            reason=start.reason or "no_residual_violations",
        )

    current = start.violations_after
    result = RepairResult(status="repaired", violations_start=current, violations_end=current)

    for round_number in range(1, max_rounds + 1):
        if _snapshot_tracked(root) is None:
            result.reason = "working_tree_not_restorable"
            break

        candidates = _violation_files(root, start.residual, language)
        if not candidates:
            result.reason = "no_candidate_files"
            break
        payload = [
            (str(path.relative_to(root)), path.read_text(encoding="utf-8", errors="replace"))
            for path in candidates
        ]
        try:
            edits = _ask_for_edits(
                client, language, start.tool, json.dumps(start.residual)[:2000], payload
            )
        except Exception as exc:  # noqa: BLE001 — a failed repair is not a failed run
            result.rounds.append(
                RepairRound(round_number, current, current, False, f"model_error: {exc}"[:200])
            )
            break

        if not edits:
            result.rounds.append(
                RepairRound(round_number, current, current, False, "model_proposed_no_edits")
            )
            break

        touched: list[str] = []
        for edit in edits:
            target = (root / str(edit.get("path", ""))).resolve()
            # A path outside the repository is never a legitimate lint fix.
            if root.resolve() not in target.parents:
                continue
            if not target.is_file():
                continue
            target.write_text(str(edit.get("content", "")), encoding="utf-8")
            touched.append(str(target.relative_to(root.resolve())))

        if not touched:
            result.rounds.append(
                RepairRound(round_number, current, current, False, "no_applicable_edits")
            )
            break

        recount = lint(root, language)
        rebuilt, _ = build_probe_image(root, _probe_profile(root))
        after = snapshot_suite(rebuilt) if rebuilt else None

        improved = recount.status == "linted" and recount.violations_after < current
        unchanged = after is not None and after.usable and after.normalized == baseline_output

        if improved and unchanged:
            result.rounds.append(
                RepairRound(
                    round_number, current, recount.violations_after, True, "verified", touched
                )
            )
            current = recount.violations_after
            result.violations_end = current
            if current == 0:
                break
            continue

        _restore(root)
        why = (
            "suite output changed"
            if not unchanged
            else f"violations did not fall ({current} -> {recount.violations_after})"
        )
        result.rounds.append(
            RepairRound(round_number, current, current, False, why, touched)
        )
        break

    if result.violations_end == 0:
        result.status = "clean"
    elif result.violations_end < result.violations_start:
        result.status = "improved"
    else:
        result.status = "unchanged"
    return result


def _probe_profile(root: Path) -> Any:
    from stress_stack.project_detector import detect_project_profile

    return detect_project_profile(root)
