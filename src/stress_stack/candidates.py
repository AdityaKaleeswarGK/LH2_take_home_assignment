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

# Mining ranks; it does not judge.
#
# Earlier versions of this module rejected a pull request that changed no test
# file, one that changed no file outside a documentation directory, and one
# whose diff exceeded the ninetieth percentile of the repository's change sizes.
# Each looked like a measurement and was actually a prediction, and each fails
# on a repository shaped differently from the sample:
#
# * a bug fix whose failing test was already committed touches no test file, and
#   its fail-before evidence is stronger than a new test's, not weaker;
# * a directory named ``docs/`` holds real, imported, tested code in plenty of
#   projects;
# * a large diff is expensive to validate, not disqualified from being a task.
#
# What survives as a rejection is only what makes a task impossible to build at
# all: no commit to materialise, no parent to diff against, and no Python
# changed for a solver to write. Everything else is a ranking signal, where
# being wrong costs a candidate a position instead of its existence, and the
# empirical gates decide.


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


# Extensions a solver could plausibly be asked to write, per ecosystem. Mining
# needs this to answer one question — "is there any code in this change for a
# golden answer to contain?" — which used to be spelled `path.endswith(".py")`
# and was the first of three places history mining assumed Python.
_SOURCE_SUFFIXES: dict[str, tuple[str, ...]] = {
    "python": (".py",),
    "go": (".go",),
    "rust": (".rs",),
    "typescript": (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"),
    "javascript": (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"),
    "c": (".c", ".h"),
    "cpp": (".c", ".cc", ".cpp", ".cxx", ".h", ".hpp"),
}


def source_suffixes(language: str) -> tuple[str, ...]:
    """What counts as source for this ecosystem, defaulting to Python's."""
    return _SOURCE_SUFFIXES.get(language, (".py",))


def is_python(path: str) -> bool:
    """Retained for callers that mean Python specifically."""
    return path.endswith(".py")


def is_source(path: str, language: str = "python") -> bool:
    return path.endswith(source_suffixes(language))


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
    *,
    language: str = "python",
) -> tuple[list[Candidate], Funnel, dict[str, Any]]:
    """Merged pull requests that changed both source and tests, ranked.

    ``language`` decides what counts as source and how a test file is read. It
    defaults to Python because that is what every existing caller means; passing
    anything else is what lets a Go or Rust repository produce history
    candidates at all, and therefore what lets it satisfy the quota's
    ``minimum: {history: 4}``. Without it those ecosystems could only ever mine
    excision candidates, capped at four, and could never ship ten tasks.
    """
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
        source_changes = [
            d for d in deltas if is_source(d.path, language) and not d.binary
        ]
        # The only structural requirement: something a solver could write. A
        # change touching none of this ecosystem's source has no code for a
        # golden answer to contain, whatever else it may be worth.
        if not source_changes:
            funnel.drop("no_source_change", identifier)
            continue

        changed_tests = [d for d in source_changes if d.path in tests or _looks_like_test(d.path)]
        changed_source = [d for d in source_changes if d not in changed_tests]

        added, modified, unparsed = _test_function_delta(
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
                "unparsed_tests": unparsed,
                "churn": sum(d.churn for d in source_changes),
            }
        )

    candidates = [_history_candidate(item, modules) for item in staged]
    _rank(candidates)
    return (
        candidates,
        funnel,
        {
            "churn_distribution": _distribution([item["churn"] for item in staged]),
            # Not a drop reason — an unparseable test file still leaves a usable
            # candidate — but it caps how much the ranking below can be trusted,
            # so it is reported rather than absorbed.
            "test_files_unparsed": sum(int(item["unparsed_tests"]) for item in staged),
            "candidates_with_unparsed_tests": sum(
                1 for item in staged if item["unparsed_tests"]
            ),
            "note": (
                "Churn is recorded for difficulty tiering and validation cost, "
                "and is not used to reject a candidate."
            ),
        },
    )


def _test_function_delta(
    repository: GitRepository, base_sha: str, head_sha: str, paths: list[str]
) -> tuple[int, int, int]:
    """How many test functions the change added, how many it rewrote, and how
    many files could not be parsed at all.

    The two are worth separating because they predict viability very
    differently. A test that did not exist before asserts behaviour that did not
    exist before, which is what a history-derived task *is*. A rewritten test
    frequently asserts exactly what it always did — glom's Python 3.12 sweep
    rewrote twenty-four of them without changing a single outcome — so it says
    almost nothing about whether there is a task here.

    Counted, never gated: a rewritten test that genuinely tightens an assertion
    is a valid task, and only the fail-before run can tell the two apart.

    The third number is the honesty term. History is parsed with the host
    interpreter, so a historical file can simply fail to parse — and an
    unparseable file yields no functions, which is indistinguishable from a
    change that added none. Since ``added_test_functions`` carries the largest
    weight in the ranking below, that silence demotes a pull request to the
    bottom of the pool for a reason nothing records.
    """
    added = modified = unparsed = 0
    for path in paths:
        before, before_error = _test_functions_at(repository, base_sha, path)
        after, after_error = _test_functions_at(repository, head_sha, path)
        if before_error or after_error:
            unparsed += 1
        added += len(set(after) - set(before))
        modified += sum(1 for name in set(after) & set(before) if after[name] != before[name])
    return added, modified, unparsed


def _test_functions_at(
    repository: GitRepository, sha: str, path: str
) -> tuple[dict[str, str], str | None]:
    """The test units a file defined at a revision, and whether it parsed.

    Python keeps going through `ast`, which is what `collected_tests` uses to
    produce the same names pytest will collect — including class-scoped ones
    (`TestX.test_y`) that a grammar-agnostic reader would not compose the same
    way. Every other ecosystem goes through tree-sitter, which is what makes a
    Go or Rust pull request rankable at all: `added_test_functions` carries the
    largest weight in `_rank`, and before this it was structurally zero for
    every non-Python repository.

    The body text is what decides "added" from "modified", so it is taken from
    the symbol's own line span rather than re-serialised.
    """
    try:
        source = repository.run(["show", f"{sha}:{path}"], record=False)
    except GitError:
        # The file did not exist at this revision. That is an answer, not a
        # failure, and must not be counted as one.
        return {}, None

    if path.endswith(".py"):
        from stress_stack.tasks import collected_tests_with_status

        return collected_tests_with_status(source)

    from stress_stack.parsers.tree_sitter_core import detect_language, parse_source_code

    if detect_language(path) is None:
        return {}, "unknown_language"
    parsed = parse_source_code(path, source)
    if parsed.has_syntax_error:
        return {}, "syntax_error"
    lines = source.splitlines()
    found: dict[str, str] = {}
    for test in parsed.tests:
        body = "\n".join(lines[test.start_line - 1 : test.end_line])
        found[test.name] = body
    return found, None


def _looks_like_test(path: str) -> bool:
    """A test file the graph never parsed still classifies a changed path.

    ``is_test`` comes from the parsed graph, which only contains files that
    parse. A test added by the very commit under consideration is present in the
    diff but absent from HEAD's graph, so a name check backs it up.

    Shared with the tree-sitter graph rather than re-spelled: the Python-only
    version here recognised `test_*.py`, `*_test.py` and `tests/`, and therefore
    filed every `calc_test.go`, `*.spec.ts` and `#[cfg(test)]` module as source.
    """
    from stress_stack.graph_multilang import _is_test_path

    return _is_test_path(path)


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


def _history_candidate(item: dict[str, Any], modules: ModuleResolver) -> Candidate:
    record = item["record"]
    source_paths: list[str] = [str(d.path) for d in item["changed_source"]]
    test_paths_changed: list[str] = [str(d.path) for d in item["changed_tests"]]
    touched_modules = sorted({modules.of(path) for path in source_paths})
    # The primary module is the one carrying the most changed lines: it is what
    # the task is *about*, and it is what diversity is counted on. A change that
    # edited only test files still needs a module, or it cannot be counted
    # against the diversity floor at all — fall back to the whole Python change
    # set rather than dropping the candidate.
    ranked_by_churn = item["changed_source"] or item["changed_tests"]
    primary = str(max(ranked_by_churn, key=lambda d: (d.churn, d.path)).path)
    if not touched_modules:
        touched_modules = [modules.of(primary)]
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
            "files_changed": len(item["deltas"]),
            "source_files_changed": len(source_paths),
            "test_files_changed": len(test_paths_changed),
            "added_test_functions": item["added_tests"],
            "modified_test_functions": item["modified_tests"],
            # Non-zero means `added_test_functions` above is a floor, not a
            # count, so this candidate's rank understates it by an unknown
            # amount.
            "unparsed_test_files": item["unparsed_tests"],
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
        if not symbol.excision_possible:
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
    """Why no task could be built here at all.

    Only two answers remain. Thin coverage, a missing docstring and a short body
    used to appear in this list; each is now a term in ``focus_score``, because
    each described glom rather than repositories in general.
    """
    if not symbol.covering_tests:
        return "no_covering_test"
    if symbol.is_test:
        return "is_itself_a_test"
    return "not_a_callable"


def _rank(candidates: list[Candidate]) -> None:
    """Score each candidate against the pool it competes in.

    Every component is normalised across the pool rather than against an
    absolute, so a repository whose changes are uniformly small still produces a
    spread instead of collapsing to a single value.
    """
    if not candidates:
        return
    if candidates[0].source == HISTORY:
        # Ranking now carries the weight the filters used to. Two signals were
        # measured against validated outcomes rather than assumed:
        #
        # New behaviour dominates because breadth does not predict viability.
        # An earlier weighting rewarded files touched and modules touched, which
        # is the exact profile of a maintenance sweep, and it put glom's
        # "Add Python 3.12" at the top of the pool — 31 files changed, not one
        # new assertion, and every designated test passing before the change.
        #
        # Recency is here because the container is built from HEAD's declared
        # Python version while the trees are historical. A 2018 commit under a
        # 3.12 interpreter fails its own suite, which the gates catch correctly
        # and expensively; ranking spends the validation budget where it is
        # likely to pay.
        weights = {
            "new_behaviour": 0.35,
            "verifier_presence": 0.15,
            "coordination": 0.10,
            "cross_module": 0.10,
            "documentation": 0.10,
            "recency": 0.20,
        }
        raw = {
            "new_behaviour": [float(c.signals["added_test_functions"]) for c in candidates],
            "verifier_presence": [float(c.signals["test_files_changed"]) for c in candidates],
            "coordination": [float(c.signals["source_files_changed"]) for c in candidates],
            "cross_module": [float(c.signals["modules_touched"]) for c in candidates],
            "documentation": [float(c.signals["body_length"]) for c in candidates],
            "recency": [_merged_ordinal(c.signals.get("merged_at")) for c in candidates],
        }
    else:
        # focus_score already weighs coverage ratio, breadth, body size and the
        # presence of a docstring against each other. Re-normalising those same
        # inputs here as separate components counted body size twice and drowned
        # the breadth penalty: glom's `_t_eval`, executed by 114 of 202 tests,
        # ranked second on the strength of being long.
        weights = {"focus": 1.0}
        raw = {"focus": [float(c.signals["focus_score"]) for c in candidates]}

    normalized = {name: _normalize(values) for name, values in raw.items()}
    for index, candidate in enumerate(candidates):
        candidate.components = {name: normalized[name][index] for name in weights}
        candidate.score = sum(
            candidate.components[name] * weight for name, weight in weights.items()
        )
    candidates.sort(key=lambda c: (-c.score, c.candidate_id))


def _merged_ordinal(merged_at: Any) -> float:
    """An ISO timestamp as a sortable number; an absent one sorts oldest."""
    text = str(merged_at or "")[:10]
    try:
        year, month, day = (int(part) for part in text.split("-"))
    except ValueError:
        return 0.0
    return year * 372.0 + month * 31.0 + day


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
