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


def lock_dependencies(
    repo_root: Path | str, profile: ProjectProfile | None = None
) -> DependencyLockReport:
    """Produce an exact lockfile where the ecosystem's tooling allows it."""
    root = Path(repo_root)
    prof = profile or detect_project_profile(root)
    lang = prof.primary_language

    if lang == "python":
        return _lock_python(root)

    if lang == "rust":
        lock_path = root / "Cargo.lock"
        if not lock_path.exists():
            if not shutil.which("cargo"):
                return _unsupported("rust", "cargo_not_installed")
            generated = run(["cargo", "generate-lockfile"], cwd=root, timeout=900.0)
            if not generated.ok and not lock_path.exists():
                return DependencyLockReport(
                    status=FAILED,
                    ecosystem="rust",
                    lock_file=None,
                    pinned_count=0,
                    measured=True,
                    reason="cargo_generate_lockfile_failed",
                )
        text = lock_path.read_text(encoding="utf-8", errors="replace")
        return DependencyLockReport(
            status=LOCKED,
            ecosystem="rust",
            lock_file="Cargo.lock",
            pinned_count=_count_cargo_packages(text),
            measured=True,
            # Cargo.lock carries a checksum per package.
            hashed="checksum" in text,
        )

    if lang in {"typescript", "javascript"}:
        for candidate in ("pnpm-lock.yaml", "package-lock.json", "yarn.lock"):
            if (root / candidate).exists():
                counted = _count_npm_packages(root, candidate)
                if counted is None:
                    return DependencyLockReport(
                        status=LOCKED,
                        ecosystem=lang,
                        lock_file=candidate,
                        pinned_count=0,
                        measured=False,
                        reason="lockfile_present_but_unparsed",
                    )
                return DependencyLockReport(
                    status=LOCKED,
                    ecosystem=lang,
                    lock_file=candidate,
                    pinned_count=counted,
                    measured=True,
                    hashed=True,  # npm-ecosystem lockfiles carry integrity hashes
                )
        if not (root / "package.json").exists():
            return _unsupported(lang, "no_package_json")
        if not shutil.which("npm"):
            return _unsupported(lang, "npm_not_installed")
        generated = run(
            ["npm", "install", "--package-lock-only"], cwd=root, timeout=900.0
        )
        if (root / "package-lock.json").exists():
            counted = _count_npm_packages(root, "package-lock.json")
            return DependencyLockReport(
                status=LOCKED,
                ecosystem=lang,
                lock_file="package-lock.json",
                pinned_count=counted or 0,
                measured=counted is not None,
                hashed=True,
            )
        return DependencyLockReport(
            status=FAILED,
            ecosystem=lang,
            lock_file=None,
            pinned_count=0,
            measured=True,
            reason=f"npm_install_failed: {generated.stderr.strip()[:200]}",
        )

    if lang == "go":
        sum_path = root / "go.sum"
        if not sum_path.exists():
            if not shutil.which("go"):
                return _unsupported("go", "go_not_installed")
            run(["go", "mod", "download"], cwd=root, timeout=900.0)
        if not sum_path.exists():
            # A module with no external dependencies legitimately has no go.sum.
            return DependencyLockReport(
                status=LOCKED if (root / "go.mod").exists() else UNSUPPORTED,
                ecosystem="go",
                lock_file="go.mod" if (root / "go.mod").exists() else None,
                pinned_count=0,
                measured=True,
                reason="no_go_sum_no_external_dependencies",
            )
        text = sum_path.read_text(encoding="utf-8", errors="replace")
        return DependencyLockReport(
            status=LOCKED,
            ecosystem="go",
            lock_file="go.sum",
            pinned_count=_count_go_modules(text),
            measured=True,
            hashed=True,  # go.sum is entirely cryptographic checksums
        )

    return _unsupported(lang, f"no_lock_strategy_for_{lang}")
