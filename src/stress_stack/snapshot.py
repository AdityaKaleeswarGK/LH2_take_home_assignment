"""Materialise the exact before/after trees for a history-derived candidate.

Three properties this module exists to guarantee:

* **The golden diff is computed between git trees**, never between working
  directories. Environment files this toolchain generates (``requirements.lock``,
  ``Dockerfile``, ``ruff.toml``) are laid over both sides *after* materialisation,
  so hygiene changes cannot contaminate the historical patch.
* **The solver bundle carries no answer.** Trees are extracted with
  ``git archive``, which emits the tree and nothing else — no ``.git``, no
  reflog, no branch tips. An agent cannot recover the fix from history because
  the history is not there.
* **Extraction is path-safe.** Archives are unpacked with the ``data`` filter,
  which rejects absolute paths, parent traversal, symlinks and device files.
"""

from __future__ import annotations

import shutil
import tarfile
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from stress_stack.errors import GitError
from stress_stack.git_repository import GitRepository

OVERLAY_FILES = ("requirements.lock", "Dockerfile", ".dockerignore", "ruff.toml")


@dataclass(frozen=True, slots=True)
class Transition:
    """The exact revision pair a history-derived task is built from."""

    head_sha: str
    base_sha: str
    parent_index: int
    head_tree: str
    base_tree: str
    is_merge: bool
    linkage_method: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class MaterializedTask:
    task_id: str
    transition: Transition
    input_dir: Path
    solution_dir: Path
    golden_diff: Path
    changed_paths: list[str]
    overlay_applied: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "transition": self.transition.to_dict(),
            "input_dir": str(self.input_dir),
            "solution_dir": str(self.solution_dir),
            "golden_diff": str(self.golden_diff),
            "changed_paths": self.changed_paths,
            "overlay_applied": self.overlay_applied,
        }


def resolve_transition(
    repository: GitRepository, head_sha: str, *, linkage_method: str = "unknown"
) -> Transition:
    """Pick the before/after revisions for a change.

    For a merge commit the first parent is the base branch as it stood before
    the change landed, which is the state a solver should start from. For a
    squashed or direct commit there is only one parent and the choice is forced.
    Both the parent index and the resolved tree hashes are recorded so the
    selection is auditable rather than implied.
    """
    parents = repository.run(["rev-list", "--parents", "-n", "1", head_sha]).split()
    if len(parents) < 2:
        raise GitError(f"Commit {head_sha} has no parent; cannot form a transition")
    base_sha = parents[1]
    return Transition(
        head_sha=parents[0],
        base_sha=base_sha,
        parent_index=0,
        head_tree=_tree_of(repository, parents[0]),
        base_tree=_tree_of(repository, base_sha),
        is_merge=len(parents) > 2,
        linkage_method=linkage_method,
    )


def materialize_tree(repository: GitRepository, sha: str, destination: Path) -> None:
    """Extract the tree at ``sha`` into ``destination``, without any git metadata."""
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".stress-archive-") as staging:
        archive = Path(staging) / "tree.tar"
        result = repository.run_bytes(
            ["archive", "--format=tar", f"--output={archive}", sha], record=False
        )
        del result
        if not archive.exists():
            raise GitError(f"git archive produced no output for {sha}")
        with tarfile.open(archive) as bundle:
            bundle.extractall(destination, filter="data")
    purge_bytecode(destination)


def purge_bytecode(root: Path) -> int:
    """Remove compiled bytecode from a materialised tree.

    A ``.pyc`` embeds the absolute path of the machine that compiled it in
    ``co_filename``, so stale bytecode makes tracebacks report foreign paths and
    turns failure classification into a function of whatever ran here earlier.
    Snapshots must be byte-for-byte what git recorded and nothing else.
    """
    removed = 0
    for cache in root.rglob("__pycache__"):
        if cache.is_dir():
            shutil.rmtree(cache, ignore_errors=True)
            removed += 1
    for compiled in list(root.rglob("*.pyc")) + list(root.rglob("*.pyo")):
        compiled.unlink(missing_ok=True)
        removed += 1
    return removed


def golden_diff(
    repository: GitRepository, transition: Transition, *, paths: list[str] | None = None
) -> str:
    """The real diff between the two trees — the task's golden answer."""
    arguments = [
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--no-renames",
        transition.base_sha,
        transition.head_sha,
    ]
    if paths:
        arguments += ["--", *paths]
    return repository.run(arguments, record=False)


def changed_paths(repository: GitRepository, transition: Transition) -> list[str]:
    output = repository.run(
        ["diff", "--name-only", transition.base_sha, transition.head_sha], record=False
    )
    return sorted(line.strip() for line in output.splitlines() if line.strip())


def apply_overlay(source_root: Path, destination: Path) -> list[str]:
    """Copy generated environment files into a materialised tree.

    Applied identically to both sides and never present in either git tree, so
    the golden diff is unaffected by anything this toolchain generated.
    """
    applied: list[str] = []
    for name in OVERLAY_FILES:
        candidate = source_root / name
        if candidate.is_file():
            shutil.copy2(candidate, destination / name)
            applied.append(name)
    return applied


def materialize(
    repository: GitRepository,
    task_id: str,
    transition: Transition,
    task_root: Path,
    *,
    overlay_from: Path | None = None,
) -> MaterializedTask:
    """Build ``input/`` and ``solution/`` for one candidate."""
    input_dir = task_root / "input"
    solution_dir = task_root / "solution"
    for directory in (input_dir, solution_dir):
        if directory.exists():
            shutil.rmtree(directory)

    materialize_tree(repository, transition.base_sha, input_dir)
    materialize_tree(repository, transition.head_sha, solution_dir)

    overlay: list[str] = []
    if overlay_from is not None:
        overlay = apply_overlay(overlay_from, input_dir)
        apply_overlay(overlay_from, solution_dir)

    diff_path = task_root / "goldenSolution.diff"
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.write_text(golden_diff(repository, transition), encoding="utf-8")

    return MaterializedTask(
        task_id=task_id,
        transition=transition,
        input_dir=input_dir,
        solution_dir=solution_dir,
        golden_diff=diff_path,
        changed_paths=changed_paths(repository, transition),
        overlay_applied=overlay,
    )


def audit_solver_bundle(input_dir: Path) -> dict[str, Any]:
    """Confirm the solver's copy cannot be used to look up the answer.

    A benchmark whose ``input/`` still carries ``.git`` measures retrieval, not
    capability: the fix is one ``git log`` away.
    """
    forbidden = {
        ".git": (input_dir / ".git").exists(),
        ".stress_stack": (input_dir / ".stress_stack").exists(),
        "solution": (input_dir / "solution").exists(),
        "evidence": (input_dir / "evidence").exists(),
        "goldenSolution.diff": (input_dir / "goldenSolution.diff").exists(),
        "goldenSolution.md": (input_dir / "goldenSolution.md").exists(),
    }
    leaked = sorted(name for name, present in forbidden.items() if present)
    patches = sorted(
        str(path.relative_to(input_dir))
        for path in input_dir.rglob("*")
        if path.suffix in {".patch", ".diff"} and path.is_file()
    )
    bytecode = sorted(
        str(path.relative_to(input_dir))
        for path in input_dir.rglob("__pycache__")
        if path.is_dir()
    )
    return {
        "clean": not leaked and not patches and not bytecode,
        "leaked_paths": leaked,
        "stray_patches": patches,
        "stale_bytecode": bytecode,
    }


def _tree_of(repository: GitRepository, sha: str) -> str:
    return repository.run(["rev-parse", f"{sha}^{{tree}}"], record=False).strip()
