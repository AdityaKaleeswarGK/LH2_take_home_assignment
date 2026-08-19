"""Every stage, in dependency order, as one command.

The order is not a preference. ``hygiene`` reformats the tree that ``graph``
parses, so it has to run first or every anchor shifts underneath the graph;
``deps`` compiles the lockfile ``container`` builds from; ``coverage`` measures
against the provisioned environment ``hygiene`` created; ``validate`` runs
inside the image ``container`` verified. Running the stages by hand in a
plausible-looking order is how a pipeline appears to work while measuring
something other than what it claims.

Each stage is recorded with its exit code and duration whether it succeeds or
not, and a stage that fails stops the run rather than letting a later stage
report on stale inputs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from stress_stack.project_detector import ProjectProfile

from stress_stack.atomic import atomic_write_json
from stress_stack.errors import StressStackError

# Optional stages are those whose absence degrades the result without
# invalidating it: enrichment needs a model, and the deliverable is defined to
# stand without one. Adjudication is the same bargain — it replaces a measured
# difficulty tier with a reasoned one, and the measured tier is still there when
# no model answers.
_OPTIONAL = frozenset({"enrich", "adjudicate"})

# Stages that only understand Python. The environment stages (hygiene, deps,
# container) and the symbol graph dispatch on the profile and work for every
# detected ecosystem; coverage and task generation still need pytest, per-test
# coverage contexts, and `ast`. Skipping them explicitly for other ecosystems is
# not a convenience: on a Go repository `coverage` failed only because pip could
# not install a Go module, which reads as a broken pipeline rather than an
# inapplicable stage. A stage that cannot apply should say so, not fail for an
# incidental reason.
# How many times a failing stage may be retried after a repair. Two, for the
# reason `RuntimeImages.max_attempts` gives: the first correction is where
# nearly all the value is, and a stage still failing after being shown its own
# error twice is not converging.
_MAX_STAGE_ATTEMPTS = 2

# Stages whose failure carries enough information to act on. The rest fail as
# they always did — a repair with nothing to repair from is just a second
# identical run, which is how a retry loop turns a clear failure into a slow one.
_REPAIRABLE = frozenset({"workflow", "workflow_measured", "hygiene", "deps", "coverage"})

_PYTHON_ONLY = frozenset(
    {
        # Enrichment and the SQLite projection both read the Python graph's
        # richer edge set.
        "enrich",
        "index",
    }
)


@dataclass
class StageResult:
    name: str
    status: str
    seconds: float
    detail: str = ""
    # Every failed attempt that preceded this verdict, with what it failed on.
    # A stage that succeeded on its second try is not the same as one that
    # succeeded outright, and a reader has no way to tell without this.
    attempts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "stage": self.name,
            "status": self.status,
            "seconds": round(self.seconds, 2),
            "detail": self.detail[:400],
        }
        if self.attempts:
            value["attempts"] = self.attempts
        return value


@dataclass
class PipelineResult:
    repository_root: str = ""
    stages: list[StageResult] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)
    # The detected ecosystem this run was processed as. Recorded because every
    # stage verdict below is only meaningful relative to it.
    profile: dict[str, Any] = field(default_factory=dict)
    # Which model each role actually resolved to. Recorded for the same reason
    # as the profile: two runs of the same repository are not comparable without
    # it, and a reasoning-class model sat in the latency-critical worker role for
    # a full run with no artifact anywhere naming it.
    models: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """No stage failed. Not the same as having produced the deliverable."""
        return all(stage.status in {"ok", "skipped", "degraded"} for stage in self.stages)

    @property
    def deliverable_complete(self) -> bool:
        """Whether tasks were actually emitted and bundled.

        A run that skips task generation because the ecosystem is unsupported
        has no failures and no deliverable. Reporting only `ok` would present
        that as a success.
        """
        produced = {
            stage.name for stage in self.stages if stage.status in {"ok", "degraded"}
        }
        return {"emit", "bundle"} <= produced

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository_root": self.repository_root,
            "ok": self.ok,
            "deliverable_complete": self.deliverable_complete,
            "seconds": round(sum(stage.seconds for stage in self.stages), 2),
            "project_profile": self.profile,
            "models": self.models,
            "stages": [stage.to_dict() for stage in self.stages],
            "manifest": self.manifest,
        }


def run_pipeline(
    source_value: str,
    *,
    cwd: Path | None = None,
    history_limit: int | None = None,
    excision_limit: int | None = None,
    repeats: int = 2,
    workers: int = 4,
    skip: tuple[str, ...] = (),
    output: str = "output",
    profile: ProjectProfile | None = None,
) -> PipelineResult:
    """Run the whole thing, from a URL or a path, and return what each stage did.

    ``profile`` is a ``ProjectProfile``. When omitted it is detected from the
    repository once ``ingest`` has produced a working tree — which is why it is
    resolved inside the loop rather than here: before ingest there may be no
    tree to look at, only a URL.

    The environment stages resolve through the doctor modules rather than
    calling the Python implementations directly. For a Python repository the
    doctors delegate straight back to those same implementations, so this is the
    identical code path with the same gates; for every other ecosystem it is the
    difference between running and not running at all.
    """
    from stress_stack.container_doctor import run_container_verification
    from stress_stack.dependency_doctor import lock_dependencies
    from stress_stack.hygiene_dispatcher import dispatch_hygiene
    from stress_stack.project_detector import detect_project_profile

    # `build_container_artifacts` and `build_dependency_artifacts` are no longer
    # imported here: the container and dependency doctors call them for Python.
    from stress_stack.graph import (
        build_adjudication_artifacts,
        build_emission_artifacts,
        build_enrichment_artifacts,
        build_bundle_artifacts,
        build_index_artifacts,
        build_selection_artifacts,
        build_validation_artifacts,
    )
    from stress_stack.ingest import ingest

    working = cwd or Path.cwd()
    result = PipelineResult()

    def _record(current: PipelineResult) -> None:
        if current.repository_root:
            atomic_write_json(
                Path(current.repository_root) / ".stress_stack" / "pipeline_run.json",
                current.to_dict(),
            )

    # Both entries are read lazily by the stage lambdas below: `source` becomes
    # the clone path once ingest runs, and `profile` is filled in at the same
    # point. A stage table built eagerly would capture the pre-ingest values.
    here: dict[str, Any] = {"source": source_value, "profile": profile}

    stages: list[tuple[str, Callable[[], Any]]] = [
        ("ingest", lambda: ingest(here["source"], cwd=working)),
        # The workflow is settled in two halves because the pipeline's own
        # dependency order forces it: hygiene reformats the tree the graph
        # parses, so its command has to be known first, while the coverage and
        # stub probes need a graph to measure against. Each half writes into the
        # same artifact.
        ("workflow", lambda: _resolve_workflow(here, pre_graph=True)),
        ("hygiene", lambda: dispatch_hygiene(here["source"], here["profile"])),
        ("deps", lambda: lock_dependencies(here["source"], here["profile"])),
        ("graph", lambda: _build_graph(here, working)),
        ("workflow_measured", lambda: _resolve_workflow(here, pre_graph=False)),
        ("coverage", lambda: _build_coverage(here, working)),
        # `testgen` used to run here. It generated tests for *uncovered*
        # symbols, but an excision candidate needs a symbol that is already
        # well covered — the coverage band excludes exactly what testgen
        # adds. Measured on glom: of ten shipped tasks and seventeen
        # eligible ones, none referenced a generated test. Against that it
        # wrote into the analysed repository, which is what made the
        # container baseline compare against a suite that no longer existed.
        # It remains available as `stress-stack testgen`; it is simply not
        # on the path to the deliverable.
        ("container", lambda: run_container_verification(here["source"], here["profile"])),
        (
            "enrich",
            # `workers` was accepted by every layer and passed by none, so
            # `MAX_WORKERS` was the only value enrichment ever ran at.
            lambda: build_enrichment_artifacts(
                here["source"], cwd=working, workers=workers
            ),
        ),
        ("index", lambda: build_index_artifacts(here["source"], cwd=working)),
        ("mine", lambda: _build_mining(here, working)),
        (
            "validate",
            lambda: build_validation_artifacts(
                here["source"],
                cwd=working,
                history_limit=history_limit,
                excision_limit=excision_limit,
                repeats=repeats,
                max_workers=workers,
            ),
        ),
        ("select", lambda: build_selection_artifacts(here["source"], cwd=working)),
        ("adjudicate", lambda: build_adjudication_artifacts(here["source"], cwd=working)),
        ("emit", lambda: build_emission_artifacts(here["source"], cwd=working)),
        (
            "bundle",
            lambda: build_bundle_artifacts(here["source"], cwd=working, output=output),
        ),
    ]
    from stress_stack.config import load_settings, role_advisories
    from stress_stack.progress import reporter

    live = reporter()

    # Resolved once, before anything runs, so the record describes the run that
    # is about to happen rather than whatever the config says afterwards.
    settings = load_settings()
    advisories = role_advisories(settings)
    result.models = {
        "resolved": dict(sorted(settings.models.items())),
        "key_source": settings.key_source,
        "configured": settings.configured,
        "advisories": advisories,
    }
    for advisory in advisories:
        live.note(f"model advisory: {advisory['detail']}")

    for position, (name, action) in enumerate(stages, start=1):
        if name in skip:
            result.stages.append(StageResult(name, "skipped", 0.0, "skipped by request"))
            live.stage_end(name, "skipped", 0.0, "skipped by request")
            continue
        language = getattr(here["profile"], "primary_language", "python")
        if language != "python" and name in _PYTHON_ONLY:
            detail = f"not implemented for {language}"
            result.stages.append(StageResult(name, "skipped", 0.0, detail))
            live.stage_end(name, "skipped", 0.0, detail)
            _record(result)
            continue
        started = time.monotonic()
        live.stage_start(name, position, len(stages))

        # A failing stage used to end the run. Several of them can be repaired
        # from the failure itself — a workflow capability whose probe rejected
        # its command can be asked again carrying the reason, and a container
        # that would not build already hands its error back to the agent one
        # level down. So a stage gets a bounded second chance, and every attempt
        # is recorded: a run that succeeded on the retry must not read like one
        # that succeeded outright.
        attempts: list[dict[str, Any]] = []
        produced: Any = None
        failure: str | None = None
        for attempt in range(1, _MAX_STAGE_ATTEMPTS + 1):
            try:
                produced = action()
                failure = _semantic_failure(name, produced)
            except StressStackError as exc:
                produced, failure = None, str(exc)
            except Exception as exc:  # noqa: BLE001 — a stage crash is a stage result
                produced, failure = None, f"{type(exc).__name__}: {exc}"
            if failure is None:
                break
            if attempt == _MAX_STAGE_ATTEMPTS or name not in _REPAIRABLE:
                break
            attempts.append({"attempt": attempt, "detail": failure[:400]})
            live.note(f"{name} failed ({failure[:80]}); repairing and retrying")
            if not _repair(name, here, failure):
                break

        stage_status = (
            "ok"
            if failure is None
            else ("degraded" if name in _OPTIONAL else "failed")
        )
        result.stages.append(
            StageResult(
                name,
                stage_status,
                time.monotonic() - started,
                failure or "",
                attempts=attempts,
            )
        )
        _announce(live, result.stages[-1])
        semantic_error = failure
        # Ingest may have cloned; every later stage must address the clone, not
        # the URL, or each one would try to clone again.
        if name == "ingest":
            root = str(getattr(produced, "repository_root", "") or "")
            if root:
                here["source"] = root
                result.repository_root = root
                # Detected once, from the tree ingest just produced, and reused
                # by every later stage. Detecting per stage would let a stage
                # that rewrites manifests change the ecosystem mid-run.
                if here["profile"] is None:
                    here["profile"] = detect_project_profile(Path(root))
                result.profile = here["profile"].to_dict()
        if name == "emit" and isinstance(produced, dict):
            result.manifest = produced
        # Persisted after every stage, not once at the end. bundle runs inside
        # this loop and reads the file, so writing it only afterwards handed the
        # report an empty stage table on a clean run — and a stale one whenever
        # an earlier run had left a file behind, which is how a since-fixed
        # `validate: failed` leaked into a later report. Recording per stage
        # also means a crashed run leaves a record of how far it reached.
        _record(result)
        if semantic_error and name not in _OPTIONAL:
            break

    if result.repository_root:
        atomic_write_json(
            Path(result.repository_root) / ".stress_stack" / "pipeline_run.json",
            result.to_dict(),
        )
    return result


def _repair(stage: str, here: dict[str, Any], failure: str) -> bool:
    """Act on a stage's own failure, and say whether anything changed.

    Only one repair exists today and it is the one that matters: every stage in
    ``_REPAIRABLE`` reads a resolved workflow capability, and a capability that
    failed can be re-resolved with its probes forced to run again — which is
    what routes a rejected default to the agent. Returning False means nothing
    was done, and the caller stops rather than re-running the same failure.
    """
    from stress_stack.openrouter import OpenRouterClient
    from stress_stack.workflow import POST_GRAPH, PRE_GRAPH, resolve_workflow

    del failure  # recorded by the caller; the probes re-derive it themselves
    root = Path(here["source"])
    if not root.is_dir():
        return False
    client = OpenRouterClient(cache_dir=root / ".stress_stack" / "cache" / "llm")
    if not client.configured:
        # Without a model the only thing a repair could do is re-probe the same
        # default, which already failed. Saying so beats retrying it.
        return False

    only = {
        "workflow": PRE_GRAPH,
        "hygiene": ("hygiene",),
        "deps": ("lock",),
        "workflow_measured": POST_GRAPH,
        "coverage": ("coverage",),
    }[stage]
    graph = None
    if stage in {"workflow_measured", "coverage"}:
        from stress_stack.graph import load_graph

        graph = load_graph(root, here["profile"])
    resolve_workflow(
        root,
        language=getattr(here["profile"], "primary_language", "python"),
        graph=graph,
        client=client,
        only=only,
        refresh=True,
    )
    return True


def _build_graph(here: dict[str, Any], working: Path) -> Any:
    """Symbol graph from `ast` for Python, from tree-sitter for anything else.

    Python keeps the existing builder: it resolves dotted imports, call edges
    and inheritance, which the tree-sitter layer does not attempt. The
    tree-sitter builder produces the same artifact files with containment and
    import edges, validated the same way.
    """
    language = getattr(here["profile"], "primary_language", "python")
    if language == "python":
        from stress_stack.graph import build_graph_artifacts

        return build_graph_artifacts(here["source"], cwd=working)

    from stress_stack.graph_multilang import build_graph_artifacts as build_multilang

    return build_multilang(here["source"])


def _language(here: dict[str, Any]) -> str:
    return getattr(here["profile"], "primary_language", "python")


def _resolve_workflow(here: dict[str, Any], *, pre_graph: bool) -> dict[str, Any]:
    """Settle how this ecosystem is formatted, locked, run and measured.

    Defaults are probed before any model is consulted, so a Python or Go
    repository resolves its whole workflow offline and this stage costs nothing
    but the probes. Where a default fails its probe, or the ecosystem has none,
    an agent reads the tree — which is the only reason a C++ repository can get
    past this stage at all.
    """
    from stress_stack.openrouter import OpenRouterClient
    from stress_stack.workflow import POST_GRAPH, PRE_GRAPH, resolve_workflow

    root = Path(here["source"])
    language = _language(here)
    graph = None
    if not pre_graph:
        from stress_stack.graph import load_graph

        graph = load_graph(root, here["profile"])

    client = OpenRouterClient(cache_dir=root / ".stress_stack" / "cache" / "llm")
    workflow = resolve_workflow(
        root,
        language=language,
        graph=graph,
        client=client if client.configured else None,
        only=PRE_GRAPH if pre_graph else POST_GRAPH,
    )
    here["workflow"] = workflow
    wanted = PRE_GRAPH if pre_graph else POST_GRAPH
    return {
        "language": language,
        "resolved": {
            name: workflow.capabilities[name].source
            for name in wanted
            if name in workflow.capabilities
        },
        "unusable": sorted(
            name
            for name in wanted
            if name not in workflow.capabilities or not workflow.capabilities[name].usable
        ),
    }


def _build_coverage(here: dict[str, Any], working: Path) -> Any:
    """Per-test coverage: coverage.py contexts for Python, one run per test elsewhere."""
    if _language(here) == "python":
        from stress_stack.graph import build_coverage_artifacts

        return build_coverage_artifacts(here["source"], cwd=working)

    from stress_stack.graph_multilang import build_coverage_artifacts as build_multilang

    return build_multilang(here["source"], _language(here))


def _build_mining(here: dict[str, Any], working: Path) -> Any:
    """Rank candidates. One miner, told which ecosystem it is reading."""
    from stress_stack.graph import build_mining_artifacts

    return build_mining_artifacts(
        here["source"], cwd=working, profile=here["profile"]
    )


def _announce(live: Any, stage: StageResult) -> None:
    live.stage_end(stage.name, stage.status, stage.seconds, stage.detail[:80])


def _semantic_failure(stage: str, produced: Any) -> str | None:
    """Turn a returned-but-unsuccessful stage result into a pipeline failure."""
    if stage == "hygiene":
        status = getattr(produced, "status", "unknown")
        # A stage that measured before/after must show a clean `complete`; one
        # that could not measure is held to `complete_unverified` instead, so it
        # cannot pass itself off as verified. Python always measures, so the
        # default of True keeps its gate exactly as strict as it was.
        if getattr(produced, "regressions_verified", True):
            if status != "complete":
                return f"hygiene status is {status}"
        elif status != "complete_unverified":
            return f"hygiene status is {status}"
    if stage == "deps":
        if hasattr(produced, "measured"):  # DependencyLockReport
            if getattr(produced, "test_environment_available", None) is False:
                return "test environment is unavailable"
            if getattr(produced, "status", None) != "locked":
                status = getattr(produced, "status", "unknown")
                reason = getattr(produced, "reason", "")
                return f"dependency lock status is {status}{f' ({reason})' if reason else ''}"
        else:  # legacy DependencyArtifacts
            lock = getattr(produced, "lock", {}) or {}
            if not getattr(produced, "environment_available", False):
                return "test environment is unavailable"
            if not str(lock.get("status") or "").startswith("locked"):
                return f"dependency lock status is {lock.get('status') or 'unknown'}"
    if stage == "container" and getattr(produced, "status", None) != "verified":
        return f"container status is {getattr(produced, 'status', 'unknown')}"
    if stage == "graph":
        validation = getattr(produced, "validation", {}) or {}
        if validation.get("status") != "verified":
            return f"graph validation status is {validation.get('status') or 'unknown'}"
    if stage in {"workflow", "workflow_measured"} and isinstance(produced, dict):
        unusable = produced.get("unusable") or []
        if unusable:
            return f"no usable workflow for: {', '.join(unusable)}"
    if stage == "coverage" and isinstance(produced, dict):
        if produced.get("coverage") != "available":
            return f"coverage is {produced.get('coverage') or 'unavailable'}"
    if stage == "validate" and isinstance(produced, dict):
        if int((produced.get("summary") or {}).get("eligible") or 0) < 10:
            return "fewer than 10 tasks passed validation"
    if stage == "adjudicate" and isinstance(produced, dict):
        # Reported rather than fatal: `adjudicate` is optional, so this surfaces
        # as `degraded` and the run continues on the measured tiers.
        if produced.get("note"):
            return str(produced["note"])
        if int(produced.get("fell_back") or 0):
            return f"{produced['fell_back']} task(s) kept the measured tier"
    if stage == "select" and isinstance(produced, dict):
        if not (produced.get("ledger") or {}).get("satisfied"):
            return "task selection did not satisfy the quota"
    if stage == "emit" and isinstance(produced, dict):
        if (
            produced.get("task_count") != 10
            or not produced.get("quota_satisfied")
            or produced.get("instructions_leaking")
            or produced.get("instructions_invalid")
        ):
            return "emitted task manifest is incomplete or unsafe"
    if stage == "bundle" and isinstance(produced, dict):
        if produced.get("missing") or produced.get("task_count") != 10:
            return "bundle is missing required artifacts or does not contain 10 tasks"
    return None
