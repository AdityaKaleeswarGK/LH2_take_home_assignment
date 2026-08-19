"""Multi-language dependency locking, reported as what was actually measured.

Locking means different things per ecosystem, and this module refuses to
flatten that difference into a uniform claim of success. A result is only
``locked`` when a lockfile exists *and* this process can count what is in it.
When the ecosystem's tool is absent, the honest answer is ``unsupported`` — a
manifest that says "0 packages pinned, tool missing" is worth more than one
that says "locked" because a code path fell through to a default.

Python delegates to the verified path in ``graph.build_dependency_artifacts``;
every other ecosystem reports only what its lockfile can be read to contain.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stress_stack.project_detector import ProjectProfile, detect_project_profile
from stress_stack.tooling import run

# A lock is `locked` only with a counted lockfile behind it. `unsupported` means
# the ecosystem's tool was unavailable, `failed` means it ran and did not
# produce a lockfile. None of these are interchangeable.
LOCKED = "locked"
UNSUPPORTED = "unsupported"
FAILED = "failed"


@dataclass
class DependencyLockReport:
    """What locking actually achieved. Named to avoid colliding with
    ``locking.LockResult``, which is the Python-specific uv result this wraps."""

    status: str
    ecosystem: str
    lock_file: str | None
    pinned_count: int
    # True only when `pinned_count` came from reading a lockfile, not a guess.
    measured: bool
    # Whether a usable test environment was provisioned. Python measures this and
    # the pipeline gates on it; ecosystems that do not probe it report None so
    # that "not checked" stays distinct from "checked and absent".
    test_environment_available: bool | None = None
    hashed: bool = False
    reason: str = ""
    unresolved: list[str] = field(default_factory=list)
    lock_data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ecosystem": self.ecosystem,
            "lock_file": self.lock_file,
            "pinned_count": self.pinned_count,
            "measured": self.measured,
            "test_environment_available": self.test_environment_available,
            "hashed": self.hashed,
            "reason": self.reason,
            "unresolved": self.unresolved,
            "lock_data": self.lock_data,
        }


def _unsupported(ecosystem: str, reason: str) -> DependencyLockReport:
    return DependencyLockReport(
        status=UNSUPPORTED,
        ecosystem=ecosystem,
        lock_file=None,
        pinned_count=0,
        measured=False,
        reason=reason,
    )


def _lock_python(root: Path) -> DependencyLockReport:
    """Wrap the verified Python path, reading its real fields.

    ``DependencyArtifacts.lock`` is ``locking.LockResult.to_dict()``, whose keys
    are status/package_count/hashed/lockfile — not runtime/test. The pins
    themselves live in ``locked_pins``, and the packages imported but absent
    from the lockfile are already computed in ``audit``.
    """
    from stress_stack.graph import build_dependency_artifacts

    report = build_dependency_artifacts(str(root))
    lock = report.lock
    lock_status = str(lock.get("status", ""))
    pins = report.locked_pins

    if not lock_status.startswith("locked"):
        return DependencyLockReport(
            status=FAILED if lock_status else UNSUPPORTED,
            ecosystem="python",
            lock_file=lock.get("lockfile"),
            pinned_count=0,
            measured=True,
            test_environment_available=report.environment_available,
            reason=str(lock.get("reason") or lock_status or "no_lock_produced"),
            unresolved=list(report.audit.get("imported_not_locked", [])),
        )

    return DependencyLockReport(
        status=LOCKED,
        ecosystem="python",
        lock_file=lock.get("lockfile") or str(report.lockfile),
        # Prefer the pins this process actually parsed out of the lockfile;
        # fall back to uv's own count only if parsing produced nothing.
        pinned_count=len(pins) or int(lock.get("package_count") or 0),
        measured=True,
        test_environment_available=report.environment_available,
        hashed=bool(lock.get("hashed")),
        unresolved=list(report.audit.get("imported_not_locked", [])),
        lock_data={
            "runtime": report.runtime,
            "test": report.test,
            "python_version": lock.get("python_version"),
        },
    )


def _count_cargo_packages(text: str) -> int:
    return text.count("[[package]]")


def _count_go_modules(text: str) -> int:
    """go.sum lists each module twice (zip and go.mod hashes) — count modules."""
    modules = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            modules.add((parts[0], parts[1].removesuffix("/go.mod")))
    return len(modules)


def _count_npm_packages(root: Path, lock_file: str) -> int | None:
    """Count entries in an npm-ecosystem lockfile, or None if unreadable."""
    path = root / lock_file
    text = path.read_text(encoding="utf-8", errors="replace")
    if lock_file == "package-lock.json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        # lockfileVersion 2/3 use "packages"; version 1 uses "dependencies".
        packages = data.get("packages")
        if isinstance(packages, dict):
            # The "" key is the root project itself, not a dependency.
            return len([k for k in packages if k])
        dependencies = data.get("dependencies")
        return len(dependencies) if isinstance(dependencies, dict) else None
    if lock_file == "pnpm-lock.yaml":
        # Entries under `packages:` are indented keys ending in a colon.
        return sum(1 for line in text.splitlines() if line.startswith("  /"))
    if lock_file == "yarn.lock":
        return sum(
            1
            for line in text.splitlines()
            if line and not line.startswith((" ", "#")) and line.rstrip().endswith(":")
        )
    return None


# How to count what a lockfile pins, keyed by the file's own name. This is the
# half that has to stay code: a count comes from parsing a format, and a number
# nobody parsed is worth less than an honest "not measured". The half that is
# now data — which command produces the file, and what it is called — lives in
# the resolved workflow, which is why a sixth ecosystem no longer needs a branch.
_COUNTERS: dict[str, Any] = {
    "Cargo.lock": lambda root, name: _count_cargo_packages(
        (root / name).read_text(encoding="utf-8", errors="replace")
    ),
    "go.sum": lambda root, name: _count_go_modules(
        (root / name).read_text(encoding="utf-8", errors="replace")
    ),
    "package-lock.json": _count_npm_packages,
    "pnpm-lock.yaml": _count_npm_packages,
    "yarn.lock": _count_npm_packages,
}

# Lockfiles that carry a cryptographic digest per entry.
_HASHED = frozenset({"go.sum", "package-lock.json", "pnpm-lock.yaml"})


def lock_dependencies(
    repo_root: Path | str,
    profile: ProjectProfile | None = None,
    *,
    client: Any = None,
) -> DependencyLockReport:
    """Produce an exact lockfile where the ecosystem's tooling allows it.

    Python delegates to the verified `uv` path, which does more than lock: it
    provisions and probes a test environment, and the pipeline gates on that.
    Every other ecosystem runs the command the workflow probed — the probe being
    that the command produces a lockfile and produces the *same* one twice, so
    a file nobody can reproduce never reaches this report as a pin.
    """
    root = Path(repo_root)
    prof = profile or detect_project_profile(root)
    lang = prof.primary_language

    if lang == "python":
        return _lock_python(root)

    from stress_stack.workflow import LOCK, load_workflow, resolve_workflow

    workflow = load_workflow(root / ".stress_stack" / "workflow.json")
    record = workflow.get(LOCK) if workflow else None
    if record is None:
        workflow = resolve_workflow(root, language=lang, client=client, only=(LOCK,))
        record = workflow.get(LOCK)
    if record is None:
        return _unsupported(lang, f"no_probed_lock_command_for_{lang}")

    lock_file = str(record.settings.get("lockfile") or "")
    argv = record.command("lock")
    path = root / lock_file if lock_file else None

    if path is not None and not path.exists() and argv:
        if not shutil.which(argv[0]):
            return _unsupported(lang, f"{argv[0]}_not_installed")
        run(argv, cwd=root, timeout=1800.0)

    if path is None or not path.exists():
        # The probe already established that this command succeeds. A command
        # that succeeds and writes no lockfile has pinned nothing, which for a
        # module with no external dependencies is the correct answer and not a
        # failure — `go mod download` says exactly that and exits zero.
        manifest_present = any(
            (root / name).exists()
            for name in ("go.mod", "Cargo.toml", "package.json", "CMakeLists.txt")
        )
        return DependencyLockReport(
            status=LOCKED if manifest_present else UNSUPPORTED,
            ecosystem=lang,
            lock_file=None,
            pinned_count=0,
            measured=True,
            reason="lock_command_succeeded_with_nothing_to_pin",
        )

    counter = _COUNTERS.get(Path(lock_file).name)
    counted = None
    if counter is not None:
        try:
            counted = counter(root, lock_file)
        except (OSError, ValueError):
            counted = None

    return DependencyLockReport(
        status=LOCKED,
        ecosystem=lang,
        lock_file=lock_file,
        pinned_count=counted or 0,
        # False where this process has no reader for the format. The lockfile is
        # real and reproducible either way; what is unmeasured is how much is in
        # it, and saying so beats reporting a zero nobody counted.
        measured=counted is not None,
        hashed=Path(lock_file).name in _HASHED
        or (Path(lock_file).name == "Cargo.lock"
            and "checksum" in path.read_text(encoding="utf-8", errors="replace")),
        reason="" if counted is not None else "lockfile_present_but_unparsed",
    )
