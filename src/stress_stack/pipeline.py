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
from typing import Any, Callable

from stress_stack.atomic import atomic_write_json
from stress_stack.errors import StressStackError

# Optional stages are those whose absence degrades the result without
# invalidating it: enrichment needs a model, and the deliverable is defined to
# stand without one.
_OPTIONAL = frozenset({"enrich"})


@dataclass
class StageResult:
    name: str
    status: str
    seconds: float
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.name,
            "status": self.status,
            "seconds": round(self.seconds, 2),
            "detail": self.detail[:400],
        }


@dataclass
class PipelineResult:
    repository_root: str = ""
    stages: list[StageResult] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(stage.status in {"ok", "skipped"} for stage in self.stages)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository_root": self.repository_root,
            "ok": self.ok,
            "seconds": round(sum(stage.seconds for stage in self.stages), 2),
            "stages": [stage.to_dict() for stage in self.stages],
            "manifest": self.manifest,
        }


def run_pipeline(
    source_value: str,
    *,
    cwd: Path | None = None,
    history_limit: int = 30,
    excision_limit: int = 12,
    repeats: int = 2,
    skip: tuple[str, ...] = (),
) -> PipelineResult:
    """Run the whole thing, from a URL or a path, and return what each stage did."""
    from stress_stack.graph import (
        build_container_artifacts,
        build_coverage_artifacts,
        build_dependency_artifacts,
        build_emission_artifacts,
        build_enrichment_artifacts,
        build_graph_artifacts,
        build_index_artifacts,
        build_mining_artifacts,
        build_selection_artifacts,
        build_validation_artifacts,
    )
    from stress_stack.hygiene import run_hygiene
    from stress_stack.ingest import ingest

    working = cwd or Path.cwd()
    result = PipelineResult()
    here = {"source": source_value}

    def at(source: str) -> Callable[[], Any]:
        return lambda: source

    stages: list[tuple[str, Callable[[], Any]]] = [
        ("ingest", lambda: ingest(here["source"], cwd=working)),
        ("hygiene", lambda: run_hygiene(here["source"], cwd=working)),
        ("deps", lambda: build_dependency_artifacts(here["source"], cwd=working)),
        ("container", lambda: build_container_artifacts(here["source"], cwd=working)),
        ("graph", lambda: build_graph_artifacts(here["source"], cwd=working)),
        ("coverage", lambda: build_coverage_artifacts(here["source"], cwd=working)),
        ("enrich", lambda: build_enrichment_artifacts(here["source"], cwd=working)),
        ("index", lambda: build_index_artifacts(here["source"], cwd=working)),
        ("mine", lambda: build_mining_artifacts(here["source"], cwd=working)),
        (
            "validate",
            lambda: build_validation_artifacts(
                here["source"],
                cwd=working,
                history_limit=history_limit,
                excision_limit=excision_limit,
                repeats=repeats,
            ),
        ),
        ("select", lambda: build_selection_artifacts(here["source"], cwd=working)),
        ("emit", lambda: build_emission_artifacts(here["source"], cwd=working)),
    ]
    del at

    for name, action in stages:
        if name in skip:
            result.stages.append(StageResult(name, "skipped", 0.0, "skipped by request"))
            continue
        started = time.monotonic()
        try:
            produced = action()
        except StressStackError as exc:
            result.stages.append(
                StageResult(name, "failed", time.monotonic() - started, str(exc))
            )
            if name in _OPTIONAL:
                result.stages[-1].status = "degraded"
                continue
            break
        except Exception as exc:  # noqa: BLE001 — a stage crash is a stage result
            result.stages.append(
                StageResult(
                    name, "failed", time.monotonic() - started, f"{type(exc).__name__}: {exc}"
                )
            )
            if name in _OPTIONAL:
                result.stages[-1].status = "degraded"
                continue
            break

        result.stages.append(StageResult(name, "ok", time.monotonic() - started))
        # Ingest may have cloned; every later stage must address the clone, not
        # the URL, or each one would try to clone again.
        if name == "ingest":
            root = str(getattr(produced, "repository_root", "") or "")
            if root:
                here["source"] = root
                result.repository_root = root
        if name == "emit" and isinstance(produced, dict):
            result.manifest = produced

    if result.repository_root:
        atomic_write_json(
            Path(result.repository_root) / ".stress_stack" / "pipeline_run.json",
            result.to_dict(),
        )
    return result
