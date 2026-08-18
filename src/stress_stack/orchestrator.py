"""Central project-aware orchestrator.

Ingests a repository, profiles its ecosystem, and runs the pipeline under that
profile. The ``hygiene``, ``deps``, and ``container`` stages resolve through the
doctor modules, which dispatch on the profile's primary language; a Python
repository routes back to the original implementations unchanged.

Known limitations, stated here rather than left for a reader to discover:

* Only the environment stages are ecosystem-aware. Mining, excision, coverage
  and validation still assume Python, so a non-Python repository gets a
  reproducible container and an honest hygiene/lock report, then produces no
  tasks. That is the next boundary to move.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stress_stack.ingest import ingest
from stress_stack.pipeline import run_pipeline
from stress_stack.project_detector import ProjectProfile, detect_project_profile

logger = logging.getLogger(__name__)


@dataclass
class OrchestratorRunResult:
    repository_root: str
    profile: ProjectProfile
    stages: list[dict[str, Any]] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)
    tasks_validated: int = 0
    tasks_selected: int = 0
    ok: bool = True
    # `ok` means nothing failed; this means tasks were actually emitted. They
    # come apart whenever task generation is skipped as unsupported.
    deliverable_complete: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository_root": self.repository_root,
            "project_profile": self.profile.to_dict(),
            "ok": self.ok,
            "deliverable_complete": self.deliverable_complete,
            "tasks_validated": self.tasks_validated,
            "tasks_selected": self.tasks_selected,
            "stages": self.stages,
            "manifest": self.manifest,
        }


def orchestrate_repository(
    source_path: str,
    *,
    max_workers: int = 4,
    history_limit: int = 30,
    excision_limit: int = 12,
    repeats: int = 2,
    output_dir: str = "output",
) -> OrchestratorRunResult:
    """Run end-to-end benchmark generation with project-aware orchestration.

    ``max_workers`` bounds how many candidates the validate stage puts through
    the gates at once. It is a disk budget rather than a CPU one — the container
    concurrency that competes for cores is bounded separately in ``sandbox`` —
    so raising it on a large repository costs working space, not throughput.
    """
    # Step 1: Ingest repository if remote URL or local path
    ingest_result = ingest(source_path, cwd=Path.cwd())
    root = Path(ingest_result.repository_root)

    # Step 2: Project & CI Doctor Discovery
    profile = detect_project_profile(root)
    logger.info(
        "Detected %s (%s); environment stages will dispatch to the %s doctors.",
        profile.primary_language,
        profile.toolchain,
        profile.primary_language,
    )

    # Step 3: Run the pipeline against that profile. Passing it rather than
    # letting the pipeline re-detect keeps the profile this result reports
    # identical to the one the stages actually ran under.
    pipe_result = run_pipeline(
        str(root),
        history_limit=history_limit,
        excision_limit=excision_limit,
        repeats=repeats,
        workers=max_workers,
        output=output_dir,
        profile=profile,
    )

    manifest = pipe_result.manifest if isinstance(pipe_result.manifest, dict) else {}
    tasks_selected = len(manifest.get("tasks") or [])
    # Selected tasks are the subset of validated ones the quotas kept, so the
    # two numbers are not interchangeable. Everything shipped passed validation,
    # and `validated_not_shipped` is the rest of what passed.
    tasks_validated = tasks_selected + len(manifest.get("validated_not_shipped") or [])

    return OrchestratorRunResult(
        repository_root=str(root),
        profile=profile,
        stages=[s.to_dict() for s in pipe_result.stages],
        manifest=manifest,
        tasks_validated=tasks_validated,
        tasks_selected=tasks_selected,
        ok=pipe_result.ok,
        deliverable_complete=pipe_result.deliverable_complete,
    )
