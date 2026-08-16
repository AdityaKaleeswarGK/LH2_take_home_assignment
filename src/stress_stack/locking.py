"""Fully pinned lockfile generation via ``uv pip compile``.

Resolution is a solved problem and hand-rolling it is how you ship a manifest
that omits the transitive closure. ``uv pip compile`` reads whatever the project
already declares (``pyproject.toml``, ``setup.py``, ``setup.cfg``, or
``requirements.in``), resolves the complete graph, and emits exact pins
annotated with what required each package. Hashes make the install verifiable.

The lock is one file containing everything needed to reproduce the environment
the test suite was verified in — runtime, test, and optional alike. A lock that
holds only direct runtime imports will not install a working test environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stress_stack.errors import ToolingError
from stress_stack.tooling import CommandResult, run

_MANIFESTS = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.in")
_TEST_EXTRA_ORDER = ("test", "tests", "testing", "dev")


@dataclass(frozen=True, slots=True)
class LockResult:
    manifest: str | None
    extras: list[str]
    lockfile: Path | None
    package_count: int
    hashed: bool
    python_version: str
    command: str | None
    status: str
    reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "0.1.0",
            "status": self.status,
            "reason": self.reason,
            "manifest": self.manifest,
            "extras": self.extras,
            "lockfile": str(self.lockfile) if self.lockfile else None,
            "package_count": self.package_count,
            "hashed": self.hashed,
            "python_version": self.python_version,
            "command": self.command,
        }


def find_manifest(root: Path) -> str | None:
    for name in _MANIFESTS:
        if (root / name).is_file():
            return name
    return None


def compile_lock(
    root: Path,
    destination: Path,
    *,
    extras: list[str] | None = None,
    python_version: str = "3.12",
    generate_hashes: bool = True,
    uv: str = "uv",
) -> LockResult:
    manifest = find_manifest(root)
    if manifest is None:
        return LockResult(
            manifest=None,
            extras=[],
            lockfile=None,
            package_count=0,
            hashed=False,
            python_version=python_version,
            command=None,
            status="skipped",
            reason="no_dependency_manifest_found",
        )

    selected = list(extras or [])
    arguments = [uv, "pip", "compile", manifest, "--python-version", python_version]
    for extra in selected:
        arguments += ["--extra", extra]
    if generate_hashes:
        arguments.append("--generate-hashes")
    arguments += ["-o", str(destination), "--quiet"]

    result = run(arguments, cwd=root, timeout=900.0)
    if not result.ok:
        retried = _retry_without_extras(
            root, destination, manifest, python_version, generate_hashes, uv, selected, result
        )
        if retried is not None:
            return retried
        return LockResult(
            manifest=manifest,
            extras=selected,
            lockfile=None,
            package_count=0,
            hashed=generate_hashes,
            python_version=python_version,
            command=" ".join(arguments),
            status="failed",
            reason=result.failure_detail(),
        )

    return LockResult(
        manifest=manifest,
        extras=selected,
        lockfile=destination,
        package_count=count_packages(destination),
        hashed=generate_hashes,
        python_version=python_version,
        command=" ".join(arguments),
        status="locked",
        reason=None,
    )


def _retry_without_extras(
    root: Path,
    destination: Path,
    manifest: str,
    python_version: str,
    generate_hashes: bool,
    uv: str,
    selected: list[str],
    failure: CommandResult,
) -> LockResult | None:
    """A declared extra that does not exist should not cost us the whole lock."""
    if not selected:
        return None
    arguments = [uv, "pip", "compile", manifest, "--python-version", python_version]
    if generate_hashes:
        arguments.append("--generate-hashes")
    arguments += ["-o", str(destination), "--quiet"]
    result = run(arguments, cwd=root, timeout=900.0)
    if not result.ok:
        return None
    return LockResult(
        manifest=manifest,
        extras=[],
        lockfile=destination,
        package_count=count_packages(destination),
        hashed=generate_hashes,
        python_version=python_version,
        command=" ".join(arguments),
        status="locked_without_extras",
        reason=f"extras {selected} rejected: {failure.failure_detail()}",
    )


def count_packages(lockfile: Path) -> int:
    try:
        content = lockfile.read_text(encoding="utf-8")
    except OSError:
        return 0
    return sum(
        1
        for line in content.splitlines()
        if line and not line.startswith((" ", "#", "-")) and "==" in line
    )


def locked_packages(lockfile: Path) -> dict[str, str]:
    """Parse ``name==version`` pairs out of a compiled lock."""
    pins: dict[str, str] = {}
    try:
        content = lockfile.read_text(encoding="utf-8")
    except OSError:
        return pins
    for line in content.splitlines():
        if not line or line.startswith((" ", "#", "-")):
            continue
        candidate = line.split("\\", 1)[0].strip()
        name, separator, version = candidate.partition("==")
        if separator and name:
            pins[name.strip()] = version.strip()
    return pins


def select_extras(declared: list[str]) -> list[str]:
    """Prefer the conventional test extra when the project declares one."""
    available = {item.lower(): item for item in declared}
    for candidate in _TEST_EXTRA_ORDER:
        if candidate in available:
            return [available[candidate]]
    return []


def ensure_uv() -> str:
    result = run(["uv", "--version"])
    if not result.ok:
        raise ToolingError(
            "uv is required for dependency locking but was not found on PATH. "
            "Install it from https://docs.astral.sh/uv/ and retry."
        )
    return "uv"
