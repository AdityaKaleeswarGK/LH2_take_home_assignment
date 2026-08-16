"""Candidate mining: the first Pipeline 3 stage.

Two sources, one shape. A history candidate is a merged pull request whose diff
touched both source and tests; an excision candidate is a symbol whose covering
tests are measured rather than guessed. Both arrive here as the same record so
every later stage — materialise, instruct, validate, select — is written once.

Three properties this module exists to hold:

* **Nothing here is inferred.** Every field is read from an artifact that a
  grader can re-derive: ``history/*.jsonl`` from git and the GitHub API,
  ``coverage_map.json`` from a measured run, ``repo_graph.json`` from a verified
  parse. No model is consulted, and none can be — mining decides what ships.
* **Thresholds come from the observed distribution, never from a constant.** A
  churn ceiling of "200 lines" is a fact about glom, not about repositories.
  The ceiling here is a percentile of this repository's own change sizes, so it
  transfers to a held-out repository without a line of new code.
* **Every rejection is recorded with its reason.** The funnel is the evidence
  that the surviving candidates were selected rather than stumbled upon, and a
  drop with no stated reason is indistinguishable from a bug.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stress_stack.coverage_map import CoverageMap
from stress_stack.errors import GitError
from stress_stack.graph import RepositoryGraph
from stress_stack.git_repository import GitRepository

HISTORY = "history"
EXCISION = "excision"

# A change in the top decile of a repository's own size distribution is a
# refactor: too broad to instruct behaviourally and too broad to attribute a
# failure to. The percentile is the threshold; the resulting line count is
# whatever this repository happens to make it.
_CHURN_PERCENTILE = 0.90

# Documentation, packaging and CI changes carry no behaviour to verify. Matched
# by extension and by directory, never by repository-specific file names.
_NON_BEHAVIOURAL_SUFFIXES = frozenset(
    {".md", ".rst", ".txt", ".cfg", ".ini", ".toml", ".yaml", ".yml", ".lock", ".in"}
)
_NON_BEHAVIOURAL_DIRECTORIES = ("docs/", ".github/", "doc/")


@dataclass(frozen=True, slots=True)
class FileDelta:
    path: str
    additions: int
    deletions: int
    binary: bool

    @property
    def churn(self) -> int:
        return self.additions + self.deletions

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "additions": self.additions,
            "deletions": self.deletions,
            "binary": self.binary,
        }


@dataclass
class Candidate:
    """One thing that might become a task, with the measurements that justify it."""

    candidate_id: str
    source: str
    subject: str
    title: str
    modules: list[str]
    primary_module: str
    signals: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0
    components: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "source": self.source,
            "subject": self.subject,
            "title": self.title,
            "modules": self.modules,
            "primary_module": self.primary_module,
            "score": round(self.score, 4),
            "components": {k: round(v, 4) for k, v in sorted(self.components.items())},
            "signals": self.signals,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Candidate:
        return cls(
            candidate_id=str(value["candidate_id"]),
            source=str(value["source"]),
            subject=str(value.get("subject") or ""),
            title=str(value.get("title") or ""),
            modules=[str(item) for item in value.get("modules") or []],
            primary_module=str(value.get("primary_module") or ""),
            signals=dict(value.get("signals") or {}),
            score=float(value.get("score") or 0.0),
            components={k: float(v) for k, v in (value.get("components") or {}).items()},
        )


def load_candidates(path: Path) -> dict[str, list[Candidate]]:
    """Re-read the mined pool. Ranking is preserved as written, never recomputed."""
    if not path.is_file():
        return {HISTORY: [], EXCISION: []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        source: [Candidate.from_dict(item) for item in (payload.get(source) or {}).get("candidates", [])]
        for source in (HISTORY, EXCISION)
    }


@dataclass
class Funnel:
    """Why each rejected candidate was rejected, counted and itemised."""

    considered: int = 0
    dropped: dict[str, list[str]] = field(default_factory=dict)

    def drop(self, reason: str, identifier: str) -> None:
        self.dropped.setdefault(reason, []).append(identifier)

    def to_dict(self) -> dict[str, Any]:
        return {
            "considered": self.considered,
            "kept": self.considered - sum(len(v) for v in self.dropped.values()),
            "dropped_counts": {k: len(v) for k, v in sorted(self.dropped.items())},
            "dropped": {k: sorted(v) for k, v in sorted(self.dropped.items())},
        }


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def numstat(repository: GitRepository, base_sha: str, head_sha: str) -> list[FileDelta]:
    """Per-file additions and deletions between two revisions.

    Read from git rather than from the ingested commit record because a merge
    commit's own numstat is empty by default — the change lives between the
    trees, not in the merge.
    """
    output = repository.run(
        ["diff", "--numstat", "--no-renames", base_sha, head_sha], record=False
    )
    deltas: list[FileDelta] = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        binary = added == "-" or removed == "-"
        deltas.append(
            FileDelta(
                path=path.strip(),
                additions=0 if binary else int(added),
                deletions=0 if binary else int(removed),
                binary=binary,
            )
        )
    return deltas


def is_python(path: str) -> bool:
    return path.endswith(".py")


def is_behavioural(path: str) -> bool:
    """Whether a changed path can carry verifiable behaviour."""
    if any(path.startswith(prefix) or f"/{prefix}" in path for prefix in _NON_BEHAVIOURAL_DIRECTORIES):
        return False
    return Path(path).suffix not in _NON_BEHAVIOURAL_SUFFIXES


def percentile(values: list[int], fraction: float) -> int:
    """Nearest-rank percentile. Empty input yields 0, one value yields itself."""
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(fraction * (len(ordered) - 1))))
    return ordered[index]


@dataclass(frozen=True, slots=True)
class ModuleResolver:
    """path -> dotted module name, answered from the verified parse.

    "What counts as a module" is the kind of question that invites a per-repo
    guess. It is answered here by the same function that anchored every symbol,
    so diversity is measured against the graph rather than against a convention.

    A path the graph has never seen — a file the change under consideration
    deleted or renamed away — still needs a name, or several unrelated deleted
    files collapse into one diversity bucket. Rather than assume a layout, the
    prefix that the graph itself strips (``src/`` in a src-layout repository,
    nothing in a flat one) is measured from known paths and reused.
    """

    known: dict[str, str]
    prefixes: tuple[str, ...]

    def of(self, path: str) -> str:
        module = self.known.get(path)
        if module:
            return module
        if not path.endswith(".py"):
            return path
        relative = path
        for prefix in self.prefixes:
            if prefix and relative.startswith(prefix):
                relative = relative[len(prefix) :]
                break
        stem = relative.removesuffix(".py").removesuffix("/__init__")
        return stem.replace("/", ".") or path


def module_index(graph: RepositoryGraph) -> ModuleResolver:
    known = {parsed.path: parsed.module for parsed in graph.files if parsed.module}
    prefixes: set[str] = set()
    for path, module in known.items():
        tail = module.replace(".", "/")
        for suffix in (f"{tail}.py", f"{tail}/__init__.py"):
            if path.endswith(suffix):
                prefixes.add(path[: len(path) - len(suffix)])
                break
    # Longest first, so `src/pkg/` is preferred over the empty prefix.
    return ModuleResolver(known=known, prefixes=tuple(sorted(prefixes, key=len, reverse=True)))


def test_paths(graph: RepositoryGraph) -> set[str]:
    return {parsed.path for parsed in graph.files if parsed.is_test}


def mine_history(
    repository: GitRepository,
    graph: RepositoryGraph,
    history_root: Path,
) -> tuple[list[Candidate], Funnel, dict[str, Any]]:
    """Merged pull requests that changed both source and tests, ranked."""
    pull_requests = read_jsonl(history_root / "pull_requests.jsonl")
    links = read_jsonl(history_root / "commit_pr_links.jsonl")
    commits = {record["sha"]: record for record in read_jsonl(history_root / "commits.jsonl")}

    by_pr: dict[int, list[str]] = {}
    for link in links:
        number = link.get("pr_number")
        sha = link.get("commit_sha")
        if isinstance(number, int) and isinstance(sha, str):
            by_pr.setdefault(number, []).append(sha)

    modules = module_index(graph)
    tests = test_paths(graph)
    funnel = Funnel()
    staged: list[dict[str, Any]] = []

    for record in sorted(pull_requests, key=lambda r: r.get("number") or 0):
        number = record.get("number")
        if not isinstance(number, int):
            continue
        if not record.get("merged_at"):
            continue
        funnel.considered += 1
        identifier = f"PR#{number}"

        head_sha = _resolve_head(repository, record, by_pr.get(number, []), commits)
        if head_sha is None:
            funnel.drop("no_commit_in_repository", identifier)
            continue
        parents = repository.run(
            ["rev-list", "--parents", "-n", "1", head_sha], record=False
        ).split()
        if len(parents) < 2:
            funnel.drop("no_parent_commit", identifier)
            continue
        base_sha = parents[1]

        deltas = numstat(repository, base_sha, head_sha)
        behavioural = [d for d in deltas if is_behavioural(d.path) and not d.binary]
        python_changes = [d for d in behavioural if is_python(d.path)]
        changed_tests = [d for d in python_changes if d.path in tests or _looks_like_test(d.path)]
        changed_source = [d for d in python_changes if d not in changed_tests]

        if not changed_source:
            funnel.drop("no_source_change", identifier)
            continue
        if not changed_tests:
            funnel.drop("no_test_change", identifier)
            continue

        added, modified = _test_function_delta(
            repository, base_sha, head_sha, [d.path for d in changed_tests]
        )
        staged.append(
            {
                "record": record,
                "head_sha": head_sha,
                "base_sha": base_sha,
                "deltas": deltas,
                "changed_source": changed_source,
                "changed_tests": changed_tests,
                "added_tests": added,
                "modified_tests": modified,
                "churn": sum(d.churn for d in behavioural),
            }
        )

    ceiling = percentile([item["churn"] for item in staged], _CHURN_PERCENTILE)
    candidates: list[Candidate] = []
    for item in staged:
        record = item["record"]
        identifier = f"PR#{record['number']}"
        if ceiling and item["churn"] > ceiling:
            funnel.drop("churn_ceiling", identifier)
            continue
        candidates.append(_history_candidate(item, modules, ceiling))

    _rank(candidates)
    return (
        candidates,
        funnel,
        {
            "churn_percentile": _CHURN_PERCENTILE,
            "churn_ceiling": ceiling,
            "churn_distribution": _distribution([item["churn"] for item in staged]),
        },
    )


def _test_function_delta(
    repository: GitRepository, base_sha: str, head_sha: str, paths: list[str]
) -> tuple[int, int]:
    """How many test functions the change added, and how many it rewrote.

    The two are worth separating because they predict viability very
    differently. A test that did not exist before asserts behaviour that did not
    exist before, which is what a history-derived task *is*. A rewritten test
    frequently asserts exactly what it always did — glom's Python 3.12 sweep
    rewrote twenty-four of them without changing a single outcome — so it says
    almost nothing about whether there is a task here.

    Counted, never gated: a rewritten test that genuinely tightens an assertion
    is a valid task, and only the fail-before run can tell the two apart.
    """
    added = modified = 0
    for path in paths:
        before = _test_functions_at(repository, base_sha, path)
        after = _test_functions_at(repository, head_sha, path)
        added += len(set(after) - set(before))
        modified += sum(1 for name in set(after) & set(before) if after[name] != before[name])
    return added, modified


def _test_functions_at(repository: GitRepository, sha: str, path: str) -> dict[str, str]:
    from stress_stack.tasks import collected_tests

    try:
        return collected_tests(repository.run(["show", f"{sha}:{path}"], record=False))
    except GitError:
        return {}


def _looks_like_test(path: str) -> bool:
    """A test file the graph never parsed still disqualifies a source change.

    ``is_test`` comes from the parsed graph, which only contains files that
    parse. A test added by the very commit under consideration is present in the
    diff but absent from HEAD's graph, so a name check backs it up.
    """
    name = Path(path).name
    return name.startswith("test_") or name.endswith("_test.py") or "/tests/" in f"/{path}"


def _resolve_head(
    repository: GitRepository,
    record: dict[str, Any],
    linked: list[str],
    commits: dict[str, dict[str, Any]],
) -> str | None:
    """The commit that landed this pull request, preferring the exact merge SHA."""
    merge_sha = record.get("merge_commit_sha")
    if isinstance(merge_sha, str) and merge_sha in commits:
        return merge_sha
    for sha in sorted(linked):
        if sha in commits:
            return sha
    if isinstance(merge_sha, str) and merge_sha:
        try:
            if repository.run(["cat-file", "-t", merge_sha], record=False).strip() == "commit":
                return merge_sha
        except GitError:
            return None
    return None


def _history_candidate(
    item: dict[str, Any], modules: ModuleResolver, ceiling: int
) -> Candidate:
    record = item["record"]
    source_paths: list[str] = [str(d.path) for d in item["changed_source"]]
    test_paths_changed: list[str] = [str(d.path) for d in item["changed_tests"]]
    touched_modules = sorted({modules.of(path) for path in source_paths})
    # The primary module is the one carrying the most changed lines: it is what
    # the task is *about*, and it is what diversity is counted on.
    primary = str(max(item["changed_source"], key=lambda d: (d.churn, d.path)).path)
    body = str(record.get("body") or "")

    return Candidate(
        candidate_id=f"pr-{record['number']}",
        source=HISTORY,
        subject=f"PR#{record['number']}",
        title=str(record.get("title") or "").strip(),
        modules=touched_modules,
        primary_module=modules.of(primary),
        signals={
            "pr_number": record["number"],
            "html_url": record.get("html_url"),
            "merged_at": record.get("merged_at"),
            "author": record.get("author"),
            "head_sha": item["head_sha"],
            "base_sha": item["base_sha"],
            "changed_paths": sorted(d.path for d in item["deltas"]),
            "source_paths": sorted(source_paths),
            "test_paths": sorted(test_paths_changed),
            "primary_path": primary,
            "churn": item["churn"],
            "churn_ceiling": ceiling,
            "files_changed": len(item["deltas"]),
            "source_files_changed": len(source_paths),
            "test_files_changed": len(test_paths_changed),
            "added_test_functions": item["added_tests"],
            "modified_test_functions": item["modified_tests"],
            "modules_touched": len(touched_modules),
            "body_length": len(body),
            "deltas": [d.to_dict() for d in item["deltas"]],
        },
    )


def mine_excision(coverage: CoverageMap, graph: RepositoryGraph) -> tuple[list[Candidate], Funnel]:
    """Symbols whose measured covering tests make a removal defensible.

    The pre-filter lives in :mod:`stress_stack.coverage_map`; this only turns
    what survived it into candidates. The real gate is empirical and runs later:
    remove the body, run the covering tests, require a behavioural failure.
    """
    modules = module_index(graph)
    funnel = Funnel()
    candidates: list[Candidate] = []

    for symbol in coverage.symbols.values():
        funnel.considered += 1
        if not symbol.excision_ready:
            funnel.drop(_excision_reason(symbol), symbol.symbol_id)
            continue
        module = modules.of(symbol.path)
        candidates.append(
            Candidate(
                candidate_id=f"excise-{symbol.symbol_id.replace('/', '_').replace('::', '-')}",
                source=EXCISION,
                subject=symbol.symbol_id,
                title=f"Reimplement {symbol.qualified_name}",
                modules=[module],
                primary_module=module,
                signals={
                    "symbol_id": symbol.symbol_id,
                    "path": symbol.path,
                    "qualified_name": symbol.qualified_name,
                    "kind": symbol.kind,
                    "body_lines": symbol.body_lines,
                    "covered_lines": symbol.covered_lines,
                    "coverage_ratio": symbol.ratio,
                    "covering_tests": sorted(symbol.covering_tests),
                    "covering_test_count": len(symbol.covering_tests),
                    "focus_score": symbol.focus_score,
                    "has_docstring": symbol.has_docstring,
                },
            )
        )

    _rank(candidates)
    return candidates, funnel


def _excision_reason(symbol: Any) -> str:
    """Which pre-filter a symbol failed, most disqualifying first."""
    if not symbol.covering_tests:
        return "no_covering_test"
    if symbol.kind not in {"function", "method"}:
        return "not_a_callable"
    if len(symbol.covering_tests) < 2:
        return "single_covering_test"
    if len(symbol.covering_tests) > symbol.covering_ceiling:
        return "infrastructure_breadth"
    if not symbol.has_docstring:
        return "no_docstring_contract"
    if symbol.body_lines < 4:
        return "body_too_small"
    return "coverage_ratio_too_low"


def _rank(candidates: list[Candidate]) -> None:
    """Score each candidate against the pool it competes in.

    Every component is normalised across the pool rather than against an
    absolute, so a repository whose changes are uniformly small still produces a
    spread instead of collapsing to a single value.
    """
    if not candidates:
        return
    if candidates[0].source == HISTORY:
        # New behaviour dominates deliberately. An earlier weighting rewarded
        # breadth — files touched, modules touched, test files touched — which
        # is the exact profile of a maintenance sweep, and it put glom's
        # "Add Python 3.12" pull request at the top of the pool with 31 files
        # changed and not one new assertion. Measured against the validated
        # outcomes, the count of *added* test functions separates a feature
        # from a sweep and breadth does not.
        weights = {
            "new_behaviour": 0.45,
            "coordination": 0.20,
            "cross_module": 0.15,
            "documentation": 0.20,
        }
        raw = {
            "new_behaviour": [float(c.signals["added_test_functions"]) for c in candidates],
            "coordination": [float(c.signals["source_files_changed"]) for c in candidates],
            "cross_module": [float(c.signals["modules_touched"]) for c in candidates],
            "documentation": [float(c.signals["body_length"]) for c in candidates],
        }
    else:
        weights = {
            "contract_clarity": 0.30,
            "coverage_depth": 0.40,
            "substance": 0.30,
        }
        raw = {
            "contract_clarity": [float(c.signals["focus_score"]) for c in candidates],
            "coverage_depth": [float(c.signals["coverage_ratio"]) for c in candidates],
            "substance": [float(c.signals["body_lines"]) for c in candidates],
        }

    normalized = {name: _normalize(values) for name, values in raw.items()}
    for index, candidate in enumerate(candidates):
        candidate.components = {name: normalized[name][index] for name in weights}
        candidate.score = sum(
            candidate.components[name] * weight for name, weight in weights.items()
        )
    candidates.sort(key=lambda c: (-c.score, c.candidate_id))


def _normalize(values: list[float]) -> list[float]:
    low, high = min(values), max(values)
    if high <= low:
        return [1.0 for _ in values]
    return [(value - low) / (high - low) for value in values]


def _distribution(values: list[int]) -> dict[str, int]:
    if not values:
        return {}
    return {
        "count": len(values),
        "min": min(values),
        "p33": percentile(values, 0.33),
        "median": percentile(values, 0.50),
        "p67": percentile(values, 0.67),
        "p90": percentile(values, 0.90),
        "max": max(values),
    }


def module_distribution(candidates: list[Candidate]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for candidate in candidates:
        counts[candidate.primary_module] = counts.get(candidate.primary_module, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))
