from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from stress_stack.adjudicate import ADJUDICATED, MEASURED, adjudicate, load_adjudication
from stress_stack.emit import merge_adjudication
from stress_stack.explore import Explorer
from stress_stack.openrouter import Completion

TASK = {
    "task_id": "pr-1",
    "title": "Resolve paths through the T namespace",
    "source": "history",
    "primary_module": "pkg.core",
    "modules": ["pkg.core"],
    "files_in_scope": ["pkg/core.py"],
    "targets": ["tests.test_core::test_resolves"],
    "signals": {"churn": 12},
}
MEASURED_DIFFICULTY = {
    "pr-1": {
        "tier": "easy",
        "justification": "Mechanical prose from factor names.",
        "factors": {"coordinated_change": 1.0, "cross_module": 1.0, "misleading_similarity": 0.0},
    }
}


def tree(root: Path) -> Path:
    task_root = root / "pr-1" / "input"
    (task_root / "pkg").mkdir(parents=True)
    (task_root / "pkg" / "core.py").write_text(
        'def resolve(path):\n    """Resolve."""\n    return path\n', encoding="utf-8"
    )
    return root


class FakeClient:
    """A model that asks for one tool, then answers, then calibrates."""

    configured = True

    def __init__(self, *, verdict: dict[str, Any], calibration: dict[str, Any] | None = None):
        self.verdict = verdict
        self.calibration = calibration
        self.json_calls: list[list[dict[str, Any]]] = []

    def converse(self, messages, *, tools, run_tool, max_turns, **kwargs):
        run_tool("read_file", {"path": "pkg/core.py"})
        return list(messages), [_completion("")]

    def complete_json(self, messages, *, schema=None, **kwargs):
        self.json_calls.append(messages)
        payload = self.verdict if self.calibration is None or "tiers" not in schema.get(
            "properties", {}
        ) else self.calibration
        return payload, _completion(json.dumps(payload))


def _completion(content: str) -> Completion:
    return Completion(
        content=content,
        model="test/model",
        cached=False,
        prompt_tokens=1,
        completion_tokens=1,
        latency_seconds=0.0,
        finish_reason="stop",
        cache_key="deadbeef",
    )


# -- the scoped tool surface ------------------------------------------------


def test_reader_refuses_paths_that_escape_the_task_tree(tmp_path: Path) -> None:
    """A shell rooted at the clone could read the fix commit. This cannot."""
    tree(tmp_path)
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("the answer", encoding="utf-8")
    explorer = Explorer(tree=tmp_path / "pr-1" / "input")

    assert "Refused" in explorer.run("read_file", {"path": "../../outside.txt"})
    assert "the answer" not in explorer.run("read_file", {"path": "../../outside.txt"})
    assert explorer.to_dict()["refused"]


def test_reader_refuses_a_symlink_pointing_out_of_the_tree(tmp_path: Path) -> None:
    """resolve() before the containment test, or a symlink walks straight out."""
    tree(tmp_path)
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("the answer", encoding="utf-8")
    (tmp_path / "pr-1" / "input" / "link.txt").symlink_to(secret)

    result = Explorer(tree=tmp_path / "pr-1" / "input").run("read_file", {"path": "link.txt"})

    assert "Refused" in result
    assert "the answer" not in result


def test_reader_reports_failures_instead_of_raising(tmp_path: Path) -> None:
    """A raised tool ends the conversation; an explained one lets it recover."""
    tree(tmp_path)
    explorer = Explorer(tree=tmp_path / "pr-1" / "input")

    assert "error" in explorer.run("grep", {"pattern": "([unclosed"}).lower()
    assert "No tool named" in explorer.run("nonexistent", {})
    assert explorer.to_dict()["tool_calls"] == 2


def test_reader_hides_git_and_caches(tmp_path: Path) -> None:
    tree(tmp_path)
    root = tmp_path / "pr-1" / "input"
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")

    assert ".git" not in Explorer(tree=root).run("list_dir", {"path": "."})


def test_a_dot_directory_above_the_tree_does_not_hide_the_tree(tmp_path: Path) -> None:
    """The staged trees live under `.stress_stack`, which hid every file.

    Testing the absolute path's parts made the leading dot in an ancestor match
    everything, so `grep` answered "No matches" and `list_dir` answered "(empty)"
    for the whole repository — while looking like honest empty results.
    """
    root = tmp_path / ".stress_stack" / "tasks" / "pr-1" / "input"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "core.py").write_text("def resolve(path):\n    return path\n", "utf-8")
    explorer = Explorer(tree=root)

    assert "pkg/core.py:1" in explorer.run("grep", {"pattern": "def resolve"})
    assert "pkg/" in explorer.run("list_dir", {"path": "."})


# -- adjudication -----------------------------------------------------------


def test_without_a_model_the_measured_tier_stands(tmp_path: Path) -> None:
    result = adjudicate(
        [TASK], MEASURED_DIFFICULTY, tasks_root=tree(tmp_path), client=None
    )

    assert result["note"] == "no_model_configured"
    assert result["adjudicated"] == 0
    assert result["fell_back"] == 1
    assert result["tiers"] == {"pr-1": "easy"}
    assert result["verdicts"][0]["origin"] == MEASURED
    assert result["verdicts"][0]["verification_state"] == "measured"


def test_an_agent_verdict_replaces_the_tier_and_keeps_the_measurement(
    tmp_path: Path,
) -> None:
    client = FakeClient(
        verdict={
            "explored": "Read pkg/core.py and found two similar resolvers.",
            "criteria": ["misleading_similar_code"],
            "tier": "hard",
            "justification": "A near-identical resolver sits beside the target.",
        }
    )

    result = adjudicate([TASK], MEASURED_DIFFICULTY, tasks_root=tree(tmp_path), client=client)
    verdict = result["verdicts"][0]

    assert verdict["tier"] == "hard"
    assert verdict["measured_tier"] == "easy"
    assert verdict["agrees_with_measurement"] is False
    assert verdict["origin"] == ADJUDICATED
    assert verdict["verification_state"] == "reasoning_unverified_label_only"
    # It looked at something before deciding.
    assert verdict["exploration"]["tool_calls"] == 1


def test_an_unusable_verdict_falls_back_rather_than_shipping_a_guess(
    tmp_path: Path,
) -> None:
    client = FakeClient(verdict={"tier": "extremely hard", "justification": "x"})

    result = adjudicate([TASK], MEASURED_DIFFICULTY, tasks_root=tree(tmp_path), client=client)

    assert result["verdicts"][0]["tier"] == "easy"
    assert result["verdicts"][0]["origin"] == MEASURED
    assert "unusable_verdict" in result["verdicts"][0]["note"]


def test_the_task_brief_never_carries_the_fix(tmp_path: Path) -> None:
    """The judge sees the pre-change tree and measurements, never the diff."""
    from stress_stack.adjudicate import task_brief

    brief = task_brief(TASK, MEASURED_DIFFICULTY["pr-1"], {})

    for marker in ("@@", "goldenSolution", "```diff", "+++ b/"):
        assert marker not in brief
    # The measurements go in as counts, never as the tier they produced: naming
    # the answer would anchor the judgement the model is being asked to make.
    assert "easy" not in brief
    assert MEASURED_DIFFICULTY["pr-1"]["justification"] not in brief
    assert "source files changed: 1" in brief


# -- merging into the emitted record ---------------------------------------


def test_merge_keeps_both_tiers_visible(tmp_path: Path) -> None:
    path = tmp_path / "adjudication.json"
    path.write_text(
        json.dumps(
            {
                "verdicts": [
                    {
                        "task_id": "pr-1",
                        "tier": "hard",
                        "justification": "Reasoned prose.",
                        "origin": ADJUDICATED,
                        "agrees_with_measurement": False,
                        "criteria": ["cross_module_reasoning"],
                        "verification_state": "reasoning_unverified_label_only",
                        "exploration": {"tool_calls": 3, "calls": [{"tool": "read_file"}]},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    merged = merge_adjudication(MEASURED_DIFFICULTY, path)["pr-1"]

    assert merged["tier"] == "hard"
    assert merged["measured_tier"] == "easy"
    assert merged["justification"] == "Reasoned prose."
    assert merged["measured_justification"] == "Mechanical prose from factor names."
    assert merged["tier_origin"] == ADJUDICATED
    # The per-call log stays in adjudication.json rather than bloating task.json.
    assert "calls" not in merged["exploration"]


def test_merge_without_an_adjudication_leaves_the_measurement_alone() -> None:
    merged = merge_adjudication(MEASURED_DIFFICULTY, None)["pr-1"]

    assert merged["tier"] == "easy"
    assert merged["tier_origin"] == MEASURED


def test_every_role_this_module_asks_for_actually_exists() -> None:
    """A role typo degrades silently, because the failure path is a fallback.

    `role="reviewer"` was wired into calibration and there is no reviewer role.
    `model_for` raised, the broad except caught it, and every run reported
    `calibration_failed` while looking like it had merely been unlucky.
    """
    import re

    from stress_stack.config import Settings

    source = Path("src/stress_stack/adjudicate.py").read_text(encoding="utf-8")
    settings = Settings()
    for role in set(re.findall(r'role="(\w+)"', source)):
        assert settings.model_for(role)


def test_load_adjudication_tolerates_a_missing_or_broken_file(tmp_path: Path) -> None:
    assert load_adjudication(tmp_path / "absent.json") == {}
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert load_adjudication(broken) == {}
