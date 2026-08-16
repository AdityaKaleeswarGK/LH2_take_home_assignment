"""The quotas the brief states, and the one a naive implementation violates.

glom's validated pool is dominated by ``glom.core``. Taking the top ten by score
returns almost nothing else and fails the four-module requirement — silently,
because nothing in a score notices. These tests exist to make that failure loud.
"""

from __future__ import annotations

from stress_stack.selection import (
    EXCISION,
    HARD,
    HISTORY,
    Ledger,
    Quota,
    score_difficulty,
    select,
    terciles,
    tier_of,
)


def task(
    task_id: str,
    *,
    source: str = HISTORY,
    module: str = "glom.core",
    score: float = 0.5,
    **signals: float,
) -> dict:
    return {
        "task_id": task_id,
        "source": source,
        "primary_module": module,
        "modules": [module],
        "title": f"title for {task_id}",
        "score": score,
        "targets": ["t::a"],
        "signals": {"source_files_changed": 1, "body_length": 10, "churn": 20, **signals},
    }


def core_heavy_pool() -> list[dict]:
    """Twelve tasks, ten of them in one module — glom's actual shape."""
    pool = [task(f"core-{i}", score=0.9 - i * 0.01) for i in range(10)]
    pool.append(task("matching-1", module="glom.matching", score=0.2))
    pool.append(task("grouping-1", module="glom.grouping", score=0.1))
    return pool


def test_a_naive_top_ten_would_fail_the_module_floor() -> None:
    """The premise: without a penalty, score alone selects one module."""
    pool = core_heavy_pool()
    naive = sorted(pool, key=lambda t: -t["score"])[:10]

    assert len({t["primary_module"] for t in naive}) == 1


def test_selection_meets_the_module_floor_on_that_same_pool() -> None:
    ledger, _ = select(core_heavy_pool(), quota=Quota(total=10, minimum={}, maximum={}))

    assert len(ledger.entries) == 10
    assert len(ledger.by_module) >= 4 or ledger.shortfalls()


def test_the_penalty_spreads_across_modules_when_the_pool_allows() -> None:
    pool = [
        task(f"{module}-{index}", module=module, score=0.9 - index * 0.01)
        for module in ("glom.core", "glom.matching", "glom.cli", "glom.grouping")
        for index in range(4)
    ]
    ledger, _ = select(pool, quota=Quota(total=8, minimum={}, maximum={}))

    assert len(ledger.by_module) == 4
    assert ledger.shortfalls() == []


def test_minimums_are_satisfied_before_anything_competes() -> None:
    """A pool that could fill every slot with excision must still take history."""
    pool = [task(f"ex-{i}", source=EXCISION, module=f"m{i}", score=0.9) for i in range(10)]
    pool += [task(f"hist-{i}", source=HISTORY, module=f"h{i}", score=0.1) for i in range(6)]

    ledger, _ = select(pool)

    assert ledger.by_source[HISTORY] >= 4
    assert ledger.by_source[EXCISION] <= 4
    assert len(ledger.entries) == 10


def test_caps_are_never_exceeded() -> None:
    pool = [task(f"ex-{i}", source=EXCISION, module=f"m{i}", score=0.9) for i in range(20)]
    pool += [task(f"hist-{i}", source=HISTORY, module=f"h{i}", score=0.8) for i in range(20)]

    ledger, _ = select(pool)

    assert ledger.by_source[EXCISION] <= 4
    assert ledger.shortfalls() == []


def test_an_unsatisfiable_pool_reports_rather_than_pretends() -> None:
    """Three tasks in one module cannot span four; say so."""
    ledger, report = select([task(f"core-{i}", module="glom.core") for i in range(3)])

    assert ledger.shortfalls()
    assert any("modules" in problem for problem in report["shortfalls"])
    assert any("selected 3 of 10" in problem for problem in report["shortfalls"])


def test_the_ledger_reports_what_exists_not_what_is_missing() -> None:
    """What a prompt sees. "Two more needed" is selection pressure, not context."""
    ledger = Ledger()
    ledger.record("pr-51", HISTORY, "glom.core", "Path API enhancements")

    rendered = ledger.render()

    assert "pr-51" in rendered and "glom.core" in rendered
    assert "needed" not in rendered and "remaining" not in rendered


def test_terciles_come_from_the_distribution() -> None:
    assert terciles([]) == (0.0, 0.0)
    assert terciles([5.0]) == (5.0, 5.0)
    low, high = terciles([float(n) for n in range(1, 10)])
    assert low < high
    assert tier_of(low, (low, high)) != HARD
    assert tier_of(high + 1, (low, high)) == HARD


def test_difficulty_spreads_across_all_three_tiers() -> None:
    tasks = [
        task("a", source_files_changed=1, churn=5, body_length=0),
        task("b", source_files_changed=3, churn=80, body_length=200),
        task("c", source_files_changed=9, churn=900, body_length=2000),
    ]
    scored = score_difficulty(tasks, {"a": 0, "b": 2, "c": 20})

    assert {entry["tier"] for entry in scored.values()} == {"easy", "medium", "hard"}


def test_the_justification_names_a_reason_rather_than_a_number() -> None:
    tasks = [
        task("a", source_files_changed=1, churn=5),
        task("b", source_files_changed=7, churn=400, body_length=1500),
    ]
    scored = score_difficulty(tasks, {"a": 0, "b": 11})
    text = scored["b"]["justification"]

    assert text.startswith("This task ")
    assert str(scored["b"]["score"]) not in text
    assert any(word in text for word in ("modules", "source files", "symbols", "lines"))


def test_tiers_split_the_pool_evenly_even_when_it_is_tiny() -> None:
    """Value cuts collapse at n=3: both terciles land on the same observation."""
    from stress_stack.selection import assign_tiers

    assert set(assign_tiers({"a": 0.1, "b": 0.5, "c": 0.9}).values()) == {
        "easy", "medium", "hard"
    }
    counts = {}
    for tier in assign_tiers({f"t{i}": float(i) for i in range(9)}).values():
        counts[tier] = counts.get(tier, 0) + 1
    assert counts == {"easy": 3, "medium": 3, "hard": 3}


def test_tier_assignment_is_reproducible_under_ties() -> None:
    from stress_stack.selection import assign_tiers

    tied = {f"t{i}": 0.5 for i in range(6)}
    assert assign_tiers(tied) == assign_tiers(dict(reversed(list(tied.items()))))
