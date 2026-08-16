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
    groups: list[str]
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
            "groups": self.groups,
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


def group_requirements(root: Path, destination: Path) -> Path | None:
    """Write a PEP 735 test group out as a requirements file uv can compile."""
    name, entries = test_dependency_group(root)
    if not entries:
        return None
    destination.write_text(
        f"# Compiled from [dependency-groups] {name}\n" + "\n".join(entries) + "\n",
        encoding="utf-8",
    )
    return destination


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
            groups=[],
            lockfile=None,
            package_count=0,
            hashed=False,
            python_version=python_version,
            command=None,
            status="skipped",
            reason="no_dependency_manifest_found",
        )

    selected = list(extras or [])
    group_name, _ = test_dependency_group(root)
    groups = [group_name] if group_name else []
    sources = [manifest]
    build_input = _build_requirements_input(root)
    if build_input is not None:
        sources.append(str(build_input))
    arguments = [uv, "pip", "compile", *sources, "--python-version", python_version]
    for extra in selected:
        arguments += ["--extra", extra]
    for group in groups:
        arguments += ["--group", group]
    if generate_hashes:
        arguments.append("--generate-hashes")
    arguments += ["-o", str(destination), "--quiet"]

    result = run(arguments, cwd=root, timeout=900.0)
    if not result.ok:
        retried = _retry_without_extras(
            root,
            destination,
            manifest,
            python_version,
            generate_hashes,
            uv,
            selected,
            groups,
            sources,
            result,
        )
        if retried is not None:
            return retried
        return LockResult(
            manifest=manifest,
            extras=selected,
            groups=groups,
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
        groups=groups,
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
    groups: list[str],
    sources: list[str],
    failure: CommandResult,
) -> LockResult | None:
    """A declared extra that does not exist should not cost us the whole lock."""
    if not selected:
        return None
    arguments = [uv, "pip", "compile", *sources, "--python-version", python_version]
    for group in groups:
        arguments += ["--group", group]
    if generate_hashes:
        arguments.append("--generate-hashes")
    arguments += ["-o", str(destination), "--quiet"]
    result = run(arguments, cwd=root, timeout=900.0)
    if not result.ok:
        return None
    return LockResult(
        manifest=manifest,
        extras=[],
        groups=groups,
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


def test_dependency_group(root: Path) -> tuple[str, list[str]]:
    """The PEP 735 group holding test dependencies, if the project uses one.

    Extras are not the only way a project states what its tests need, and
    reading only extras made a whole convention invisible: click declares
    ``tests = ["pytest"]`` under ``[dependency-groups]`` and nothing under
    ``[project.optional-dependencies]``, so the compiled lock was empty and the
    image was built without a test runner. Group names are matched by the same
    convention already used for extras rather than by a per-project list.
    """
    manifest = root / "pyproject.toml"
    if not manifest.is_file():
        return "", []
    try:
        import tomllib

        parsed = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", []

    groups = parsed.get("dependency-groups")
    if not isinstance(groups, dict):
        return "", []
    available = {str(name).lower(): str(name) for name in groups}
    for candidate in _TEST_EXTRA_ORDER:
        if candidate in available:
            name = available[candidate]
            entries = [item for item in groups[name] if isinstance(item, str)]
            return name, entries
    return "", []


def _build_requirements_input(root: Path) -> Path | None:
    """Materialize harness and build requirements into the reproducible lock.

    ``pip install --no-deps .`` still invokes a PEP 517 backend. Installing the
    backend from a hash-pinned lock and disabling build isolation prevents that
    final build step from resolving setuptools/flit/hatchling from the network.
    """
    # The runner is part of the execution environment even when a repository
    # forgot to declare it. Pinning it here avoids a network-resolved range in
    # the generated Dockerfile.
    requirements: list[str] = ["pytest>=8,<10"]
    manifest = root / "pyproject.toml"
    if manifest.is_file():
        try:
            import tomllib

            parsed = tomllib.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            parsed = {}
        declared = (parsed.get("build-system") or {}).get("requires") or []
        requirements += [str(item) for item in declared if isinstance(item, str)]
    elif (root / "setup.py").is_file() or (root / "setup.cfg").is_file():
        # PEP 517's specified legacy default when no [build-system] exists.
        requirements.append("setuptools>=40.8.0")
    destination = root / ".stress_stack" / "locking" / "build-requirements.in"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(requirements) + "\n", encoding="utf-8")
    return destination


def ensure_uv() -> str:
    result = run(["uv", "--version"])
    if not result.ok:
        raise ToolingError(
            "uv is required for dependency locking but was not found on PATH. "
            "Install it from https://docs.astral.sh/uv/ and retry."
        )
    return "uv"
