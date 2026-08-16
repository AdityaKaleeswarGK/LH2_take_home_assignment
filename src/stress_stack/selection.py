"""Choose the ten tasks that ship, and label how hard each one is.

Two jobs live here because they share the same inputs and the same rule: both
are computed, and neither asks a model anything. Selection decides which
validated tasks ship, which the brief constrains directly; difficulty decides a
label the brief grades the reasoning of. A model may later phrase either — it
may not determine them.

The quotas are the brief's, enforced here rather than hoped for:

* ten tasks in total;
* at least four derived from repository history;
* at most four by excision, at most three net-new;
* spanning at least four distinct modules.

The diversity floor is the one that a naive implementation silently violates.
glom's validated pool is dominated by ``glom.core``; taking the top ten by score
returns nine core tasks and fails the four-module requirement outright. So the
objective carries a penalty for reusing a module, and the floor is verified
afterwards rather than assumed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

HISTORY = "history"
EXCISION = "excision"
NET_NEW = "net_new"

EASY, MEDIUM, HARD = "easy", "medium", "hard"


@dataclass(frozen=True, slots=True)
class Quota:
    """What the brief permits. Every field is a requirement, not a preference."""

    total: int = 10
    minimum: dict[str, int] = field(default_factory=lambda: {HISTORY: 4})
    maximum: dict[str, int] = field(default_factory=lambda: {EXCISION: 4, NET_NEW: 3})
    minimum_modules: int = 4

    def cap(self, source: str) -> int:
        return self.maximum.get(source, self.total)

    def floor(self, source: str) -> int:
        return self.minimum.get(source, 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "minimum": dict(sorted(self.minimum.items())),
            "maximum": dict(sorted(self.maximum.items())),
            "minimum_modules": self.minimum_modules,
        }


@dataclass
class Ledger:
    """A running count of what has been selected, and what the quotas still allow.

    This is the object the instruction prompt is shown. It deliberately reports
    what has been *created* rather than what is still *needed*: "six tasks
    exist, here are their titles and modules" helps a writer avoid repeating
    itself across the set, whereas "two more are needed from glom.matching"
    is selection pressure leaking into generated prose, and a model told what
    is missing will start shading instructions toward filling the gap.
    """

    quota: Quota = field(default_factory=Quota)
    entries: list[dict[str, Any]] = field(default_factory=list)

    @property
    def by_source(self) -> Counter[str]:
        return Counter(entry["source"] for entry in self.entries)

    @property
    def by_module(self) -> Counter[str]:
        return Counter(entry["module"] for entry in self.entries)

    @property
    def full(self) -> bool:
        return len(self.entries) >= self.quota.total

    def accepts(self, source: str) -> bool:
        return not self.full and self.by_source[source] < self.quota.cap(source)

    def record(
        self,
        task_id: str,
        source: str,
        module: str,
        title: str,
        modules: list[str] | None = None,
    ) -> None:
        self.entries.append(
            {
                "task_id": task_id,
                "source": source,
                "module": module,
                "modules": sorted(set(modules or [module])),
                "title": title,
            }
        )

    @property
    def modules_covered(self) -> set[str]:
        """Every module any selected task touches — what the floor is counted on.

        A cross-module change genuinely exercises both modules, so both count.
        The penalty below deliberately uses the *primary* module instead, which
        is what stops one sprawling pull request satisfying the four-module
        requirement single-handedly.
        """
        covered: set[str] = set()
        for entry in self.entries:
            covered.update(entry.get("modules") or [entry["module"]])
        return covered

    def shortfalls(self) -> list[str]:
        """Every quota this ledger does not yet satisfy, stated plainly."""
        problems: list[str] = []
        if len(self.entries) != self.quota.total:
            problems.append(f"selected {len(self.entries)} of {self.quota.total}")
        for source, floor in sorted(self.quota.minimum.items()):
            if self.by_source[source] < floor:
                problems.append(f"{source}: {self.by_source[source]} of at least {floor}")
        for source, cap in sorted(self.quota.maximum.items()):
            if self.by_source[source] > cap:
                problems.append(f"{source}: {self.by_source[source]} exceeds {cap}")
        modules = len(self.modules_covered)
        if modules < self.quota.minimum_modules:
            problems.append(f"modules: {modules} of at least {self.quota.minimum_modules}")
        return problems

    def render(self) -> str:
        """The created-so-far context a prompt is given."""
        if not self.entries:
            return "(no tasks written yet)"
        return "\n".join(
            f"- {entry['task_id']} [{entry['source']}, {entry['module']}]: {entry['title']}"
            for entry in self.entries
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "quota": self.quota.to_dict(),
            "selected": len(self.entries),
            "by_source": dict(sorted(self.by_source.items())),
            "by_primary_module": dict(sorted(self.by_module.items())),
            "modules_covered": sorted(self.modules_covered),
            "distinct_modules": len(self.modules_covered),
            "shortfalls": self.shortfalls(),
            "satisfied": not self.shortfalls(),
            "entries": self.entries,
        }


# --------------------------------------------------------------------------
# Difficulty
# --------------------------------------------------------------------------

# The brief names what counts: "cross-module reasoning, business-logic
# knowledge, misleading similar code, coordinated multi-file changes". Each maps
# to something already measured, and each is weighted equally rather than tuned,
# because there is no evidence available for preferring one over another.
_FACTORS = (
    "coordinated_change",
    "cross_module",
    "misleading_similarity",
    "business_logic",
    "scale",
)

_FACTOR_SENTENCE = {
    "coordinated_change": "requires coordinated edits across {source_files} source files",
    "cross_module": "spans {modules} modules, so the change cannot be reasoned about locally",
    "misleading_similarity": (
        "the repository defines {lookalikes} similarly named symbols elsewhere, so the "
        "right one has to be identified rather than searched for"
    ),
    "business_logic": (
        "depends on rules recorded in the change's description rather than inferable "
        "from the surrounding code"
    ),
    "scale": "touches {churn} lines with {verifiers} tests that must end up passing",
}


def factor_values(task: dict[str, Any], lookalikes: int) -> dict[str, float]:
    """Raw, unnormalised measurements for one task, from artifacts only."""
    signals = task.get("signals") or {}
    return {
        "coordinated_change": float(signals.get("source_files_changed") or 1),
        "cross_module": float(len(task.get("modules") or []) or 1),
        "misleading_similarity": float(lookalikes),
        "business_logic": float(signals.get("body_length") or 0),
        "scale": float(signals.get("churn") or signals.get("body_lines") or 0)
        + float(len(task.get("targets") or [])),
    }


def terciles(values: list[float]) -> tuple[float, float]:
    """The two cuts, taken from the observed distribution rather than declared.

    A fixed threshold such as "hard is over 200 lines" describes one repository.
    The brief warns that hard-coded fixes scoring well on the sample score badly
    on the held-out repo, and a tercile transfers without a line of new code.
    """
    if not values:
        return 0.0, 0.0
    ordered = sorted(values)
    if len(ordered) < 3:
        return ordered[0], ordered[-1]

    def at(fraction: float) -> float:
        index = max(0, min(len(ordered) - 1, round(fraction * (len(ordered) - 1))))
        return ordered[index]

    return at(1 / 3), at(2 / 3)


def tier_of(score: float, cuts: tuple[float, float]) -> str:
    """Which tier a score falls in, given value cuts. Reporting only."""
    low, high = cuts
    if score <= low:
        return EASY
    if score <= high:
        return MEDIUM
    return HARD


def assign_tiers(scores: dict[str, float]) -> dict[str, str]:
    """Split the pool into three equal parts by rank, not by value.

    Value thresholds collapse on a small pool: with three tasks, the lower and
    upper tercile land on the same observation, and the middle tier can never be
    assigned at all. Ranking is also what the design intends — an even split
    across easy, medium and hard — and it stays even however the composite
    happens to be distributed.

    Ties are broken by task id so the assignment is reproducible rather than
    dependent on dictionary ordering.
    """
    if not scores:
        return {}
    ordered = sorted(scores, key=lambda task_id: (scores[task_id], task_id))
    total = len(ordered)
    tiers: dict[str, str] = {}
    for position, task_id in enumerate(ordered):
        third = position * 3 // total
        tiers[task_id] = (EASY, MEDIUM, HARD)[min(2, third)]
    return tiers


def justify(task: dict[str, Any], factors: dict[str, float], ranked: Sequence[str]) -> str:
    """State the reason, not the number.

    The brief is explicit that "we are grading the reasoning, not a measured
    score", so the sentence names what makes the task hard. Restating the
    composite would be a number pretending to be an argument.
    """
    signals = task.get("signals") or {}
    context = {
        "source_files": int(factors["coordinated_change"]),
        "modules": int(factors["cross_module"]),
        "lookalikes": int(factors["misleading_similarity"]),
        "churn": int(signals.get("churn") or signals.get("body_lines") or 0),
        "verifiers": len(task.get("targets") or []),
    }
    # A factor only earns a sentence if its raw value says something. Ranking by
    # *normalised* value alone produced "requires coordinated edits across 1
    # source files" on a task graded hard — top of a normalised column can still
    # be a raw value of one, which is not a reason.
    notable = {
        "coordinated_change": context["source_files"] >= 2,
        "cross_module": context["modules"] >= 2,
        "misleading_similarity": context["lookalikes"] >= 1,
        "business_logic": factors.get("business_logic", 0.0) >= 200,
        "scale": context["churn"] >= 40 or context["verifiers"] >= 3,
    }
    clauses = [
        _FACTOR_SENTENCE[name].format(**context) for name in ranked if notable.get(name)
    ][:2]
    if not clauses:
        clauses = [
            f"is a contained change verified by {context['verifiers']} "
            f"test{'' if context['verifiers'] == 1 else 's'}"
        ]
    return "This task " + "; and it ".join(clauses) + "."


def score_difficulty(
    tasks: list[dict[str, Any]], lookalikes: dict[str, int]
) -> dict[str, dict[str, Any]]:
    """Assign every task a tier and a reason, normalised across the pool."""
    if not tasks:
        return {}

    raw = {
        task["task_id"]: factor_values(task, lookalikes.get(task["task_id"], 0))
        for task in tasks
    }
    normalised: dict[str, dict[str, float]] = {task["task_id"]: {} for task in tasks}
    for factor in _FACTORS:
        values = [raw[task["task_id"]][factor] for task in tasks]
        low, high = min(values), max(values)
        for task in tasks:
            value = raw[task["task_id"]][factor]
            normalised[task["task_id"]][factor] = (
                1.0 if high <= low else (value - low) / (high - low)
            )

    composite = {
        task["task_id"]: sum(normalised[task["task_id"]].values()) / len(_FACTORS)
        for task in tasks
    }
    cuts = terciles(list(composite.values()))
    tiers = assign_tiers(composite)

    result: dict[str, dict[str, Any]] = {}
    for task in tasks:
        task_id = task["task_id"]
        ranked = sorted(_FACTORS, key=lambda name: -normalised[task_id][name])
        result[task_id] = {
            "tier": tiers[task_id],
            "score": round(composite[task_id], 4),
            "factors": {name: round(value, 4) for name, value in sorted(raw[task_id].items())},
            "dominant_factors": ranked[:2],
            "justification": justify(task, raw[task_id], ranked),
            "tercile_cuts": [round(cuts[0], 4), round(cuts[1], 4)],
        }
    return result


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------

DIVERSITY_PENALTY = 0.35


def select(
    eligible: list[dict[str, Any]],
    *,
    quota: Quota | None = None,
    penalty: float = DIVERSITY_PENALTY,
) -> tuple[Ledger, dict[str, Any]]:
    """Greedy selection under the quotas, penalising module repetition.

    Minimums are satisfied before anything else competes for a slot: a pool that
    can fill ten slots with excision tasks would otherwise do so and leave the
    four-history requirement unmet with nothing left to fix it.
    """
    quota = quota or Quota()
    ledger = Ledger(quota=quota)
    remaining = {task["task_id"]: task for task in eligible}
    trace: list[dict[str, Any]] = []

    def take(task: dict[str, Any], reason: str) -> None:
        ledger.record(
            task_id=task["task_id"],
            source=task["source"],
            module=task.get("primary_module") or "(unknown)",
            title=task.get("title") or task["task_id"],
            modules=task.get("modules") or None,
        )
        trace.append(
            {
                "task_id": task["task_id"],
                "reason": reason,
                "module": task.get("primary_module"),
                "adjusted": round(_adjusted(task, ledger, penalty), 4),
            }
        )
        remaining.pop(task["task_id"], None)

    # 1. Required minimums, best first.
    for source, floor in sorted(quota.minimum.items()):
        while ledger.by_source[source] < floor:
            best = _best(remaining.values(), ledger, penalty, source=source)
            if best is None:
                break
            take(best, f"minimum_{source}")

    # 2. Everything else competes, subject to the caps.
    while not ledger.full:
        best = _best(remaining.values(), ledger, penalty)
        if best is None:
            break
        take(best, "ranked")

    # 3. The diversity floor is verified, then repaired if it can be.
    repairs = _repair_modules(ledger, remaining, quota)

    return ledger, {
        "penalty": penalty,
        "trace": trace,
        "repairs": repairs,
        "shortfalls": ledger.shortfalls(),
    }


def _adjusted(task: dict[str, Any], ledger: Ledger, penalty: float) -> float:
    """Score less a penalty for each time this module has already been taken."""
    module = task.get("primary_module") or "(unknown)"
    return float(task.get("score") or 0.0) - penalty * ledger.by_module[module]


def _best(
    tasks: Any, ledger: Ledger, penalty: float, *, source: str | None = None
) -> dict[str, Any] | None:
    candidates = [
        task
        for task in tasks
        if (source is None or task["source"] == source) and ledger.accepts(task["source"])
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda task: (_adjusted(task, ledger, penalty), task["task_id"]))


def _repair_modules(
    ledger: Ledger, remaining: dict[str, dict[str, Any]], quota: Quota
) -> list[dict[str, Any]]:
    """Trade a duplicate module for an unrepresented one, if the pool allows.

    Only ever swaps within the same source, so a repair cannot break a quota it
    was not asked to fix. If no such trade exists the floor stays unmet and
    ``shortfalls`` says so — reporting the miss is honest, and quietly shipping
    nine core tasks is not.
    """
    repairs: list[dict[str, Any]] = []
    while len(ledger.modules_covered) < quota.minimum_modules:
        covered = ledger.modules_covered
        incoming = max(
            (
                task
                for task in remaining.values()
                if not set(task.get("modules") or [task.get("primary_module")]) <= covered
            ),
            key=lambda task: float(task.get("score") or 0.0),
            default=None,
        )
        if incoming is None:
            break
        duplicates = [
            entry
            for entry in ledger.entries
            if ledger.by_module[entry["module"]] > 1 and entry["source"] == incoming["source"]
        ]
        if not duplicates:
            break
        outgoing = min(duplicates, key=lambda entry: entry["task_id"])
        ledger.entries.remove(outgoing)
        ledger.record(
            task_id=incoming["task_id"],
            source=incoming["source"],
            module=incoming.get("primary_module") or "(unknown)",
            title=incoming.get("title") or incoming["task_id"],
            modules=incoming.get("modules") or None,
        )
        remaining.pop(incoming["task_id"], None)
        repairs.append(
            {
                "dropped": outgoing["task_id"],
                "dropped_module": outgoing["module"],
                "added": incoming["task_id"],
                "added_module": incoming.get("primary_module"),
            }
        )
    return repairs
