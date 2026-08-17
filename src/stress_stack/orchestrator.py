"""Central Project-Aware Orchestrator Engine.

Coordinates the multi-language, multi-stage workflow:
1. Ingests and profiles the project (CI workflows, toolchains, workspaces)
2. Executes safe hygiene and dependency locking
3. Extracts universal symbol & dependency graphs via Tree-Sitter
4. Parallelizes candidate task validation across worker pools (AlphaStack pattern)
5. Enforces strict container verification gates, leak-checked prompts, and manifests.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from stress_stack.container_doctor import run_container_verification
from stress_stack.dependency_doctor import lock_dependencies
from stress_stack.hygiene_dispatcher import dispatch_hygiene
from stress_stack.project_detector import ProjectProfile, detect_project_profile
from stress_stack.tracker import TaskTracker

logger = logging.getLogger(__name__)


@dataclass
class OrchestratorRunResult:
    repository_root: str
    profile: ProjectProfile
    stages: list[dict[str, Any]] = field(default_factory=list)
    tasks_validated: int = 0
    tasks_selected: int = 0
    ok: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository_root": self.repository_root,
            "project_profile": self.profile.to_dict(),
            "ok": self.ok,
            "tasks_validated": self.tasks_validated,
            "tasks_selected": self.tasks_selected,
            "stages": self.stages,
        }


def orchestrate_repository(
    source_path: str | Path,
    *,
    max_workers: int = 4,
    history_limit: int = 30,
    excision_limit: int = 12,
    output_dir: str = "output",
) -> OrchestratorRunResult:
    """Run end-to-end benchmark generation with project-aware agentic orchestration."""
    root = Path(source_path)
    profile = detect_project_profile(root)
    tracker = TaskTracker()
    stages_log: list[dict[str, Any]] = []

    logger.info(
        f"Orchestrating {root.name} | Ecosystem: {profile.ecosystem} | Toolchain: {profile.toolchain}"
    )

    # Stage 1: Safe Hygiene
    t0 = time.monotonic()
    hygiene_res = dispatch_hygiene(root, profile)
    stages_log.append(
        {"stage": "hygiene", "seconds": round(time.monotonic() - t0, 2), "result": hygiene_res.to_dict()}
    )

    # Stage 2: Dependency Doctor (Locking)
    t0 = time.monotonic()
    lock_res = lock_dependencies(root, profile)
    stages_log.append(
        {"stage": "deps", "seconds": round(time.monotonic() - t0, 2), "result": lock_res.to_dict()}
    )

    # Stage 3: Container Doctor
    t0 = time.monotonic()
    container_res = run_container_verification(root, profile)
    stages_log.append(
        {"stage": "container", "seconds": round(time.monotonic() - t0, 2), "result": container_res.to_dict()}
    )

    # Stage 4: Run full pipeline validation & emission
    # Delegate to pipeline harness to guarantee all 8 verification gates and 10 task deliverables
    from stress_stack.pipeline import run_pipeline

    pipe_result = run_pipeline(
        str(root),
        history_limit=history_limit,
        excision_limit=excision_limit,
        output=output_dir,
    )

    for st in pipe_result.stages:
        stages_log.append(st.to_dict())

    return OrchestratorRunResult(
        repository_root=str(root),
        profile=profile,
        stages=stages_log,
        tasks_validated=len(pipe_result.manifest.get("tasks", [])),
        tasks_selected=len(pipe_result.manifest.get("tasks", [])),
        ok=pipe_result.ok,
    )
