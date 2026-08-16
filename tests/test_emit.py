from __future__ import annotations

import json
from types import SimpleNamespace

from stress_stack.emit import emit_bundle


def test_model_failure_falls_back_without_crashing(monkeypatch, tmp_path) -> None:
    task_root = tmp_path / "tasks" / "pr-1"
    (task_root / "input").mkdir(parents=True)
    (task_root / "solution").mkdir()
    (task_root / "verifier").mkdir()
    (task_root / "evidence").mkdir()
    (task_root / "input" / "m.py").write_text("def f():\n    return 1\n")
    (task_root / "goldenSolution.diff").write_text("+def f():\n+    return 2\n")
    eligible = [
        {
            "task_id": "pr-1",
            "source": "history",
            "title": "Return the updated value",
            "primary_module": "m",
            "modules": ["m"],
            "files_in_scope": ["m.py"],
            "targets": ["tests.test_m::test_value"],
            "verifier_files": ["tests/test_m.py"],
            "eligible": True,
            "signals": {"pr_number": 1},
            "detail": {"transition": {"head_sha": "b", "base_sha": "a"}, "repeats": 2},
            "gates": [],
            "runs": [],
        }
    ]
    selection = {
        "task_ids": ["pr-1"],
        "difficulty": {"pr-1": {"tier": "easy", "justification": "Focused change."}},
        "pool_size": 1,
        "ledger": {
            "by_source": {"history": 1},
            "by_primary_module": {"m": 1},
            "modules_covered": ["m"],
            "distinct_modules": 1,
            "quota": {},
            "satisfied": False,
            "shortfalls": ["fixture selection"],
        },
    }
    monkeypatch.setattr(
        "stress_stack.emit.generated_instruction",
        lambda *args, **kwargs: (None, {"fell_back": "model_error"}),
    )

    manifest = emit_bundle(
        selection,
        eligible,
        SimpleNamespace(files=[]),
        tmp_path / "tasks",
        tmp_path / "tasks.json",
        client=object(),
    )

    record = json.loads((task_root / "task.json").read_text())
    assert manifest["task_count"] == 1
    assert record["instruction_origin"] == "mechanical_model_fallback"
    assert record["generation"]["fell_back"] == "model_error"

