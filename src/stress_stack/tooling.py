"""Running other people's programs, with only what they need in the environment.

``run`` executes package managers, formatters, linters and test runners against
a repository this pipeline did not write. Several of those *are* the repository:
``pip install -e .`` executes ``setup.py``, ``npm install`` executes lifecycle
scripts, and collecting a suite imports ``conftest.py``. Each of those runs
arbitrary code from the analysed tree, on the host, outside any container.

So the environment is an allowlist rather than a copy. ``sandbox.py`` already
says containers inherit nothing from the host and passes only what it names;
that was true inside the container and false on every host path leading up to
one, which left ``OPENROUTER_API_KEY``, ``AWS_*`` and ``SSH_AUTH_SOCK`` visible
to a ``conftest.py`` during hygiene, locking and coverage — the three stages
that run a repository's own tests outside a container.

What is forwarded is what a toolchain cannot work without: a path, a home for
its caches, the caches themselves, TLS trust, and the proxy settings a registry
fetch needs. Credentials are not on the list, and adding one should have to be
argued for.

``inherit_env=True`` restores the old behaviour for a caller that needs it. No
caller that can reach repository-authored code may use it.

``git_repository`` keeps its own full environment deliberately: git needs
``SSH_AUTH_SOCK`` and ``GIT_*`` to clone a private repository, and a fresh clone
carries no hooks, so cloning is not a path from repository content to execution.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from stress_stack.errors import ToolingError

RUFF_VERSION = "0.15.7"

# Names forwarded to a subprocess when the host has them set; everything else is
# dropped. Grouped by what breaks without them, because that is the only
# argument for adding a name here.
_ENVIRONMENT_ALLOWLIST = frozenset(
    {
        # Finding and running a program at all.
        "PATH", "HOME", "TMPDIR", "TEMP", "TMP", "SHELL", "USER", "LOGNAME",
        # Windows equivalents; inert elsewhere.
        "SYSTEMROOT", "COMSPEC", "PATHEXT", "USERPROFILE",
        # Toolchain locations and caches. Dropping these breaks no build, it
        # only makes every build download the world again.
        "GOPATH", "GOCACHE", "GOMODCACHE", "GOFLAGS", "GOPROXY", "GOTOOLCHAIN",
        "CARGO_HOME", "RUSTUP_HOME", "RUSTUP_TOOLCHAIN",
        "npm_config_cache", "NPM_CONFIG_CACHE", "NODE_PATH",
        "PIP_CACHE_DIR", "UV_CACHE_DIR", "XDG_CACHE_HOME", "XDG_DATA_HOME",
        "JAVA_HOME", "MAVEN_OPTS", "GRADLE_USER_HOME",
        # Talking to the Docker daemon. Every container gate needs these.
        "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY",
        "DOCKER_BUILDKIT", "DOCKER_DEFAULT_PLATFORM", "BUILDKIT_PROGRESS",
        # TLS trust and proxies. Without these a package manager cannot reach a
        # registry on a corporate network. A proxy URL can itself carry
        # credentials, which is a real cost and the reason it is named here
        # rather than forwarded silently.
        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "no_proxy",
    }
)


def base_environment(*, inherit: bool = False) -> dict[str, str]:
    """The environment a subprocess starts from, before a caller's additions."""
    if inherit:
        return os.environ.copy()
    return {
        name: value
        for name, value in os.environ.items()
        if name in _ENVIRONMENT_ALLOWLIST
    }



@dataclass(frozen=True, slots=True)
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def failure_detail(self) -> str:
        detail = self.stderr.strip() or self.stdout.strip()
        return detail.splitlines()[-1] if detail else f"exit code {self.exit_code}"


def run(
    arguments: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    inherit_env: bool = False,
) -> CommandResult:
    environment = base_environment(inherit=inherit_env)
    environment.update({"LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1"})
    if env:
        environment.update(env)
    try:
        completed = subprocess.run(
            arguments,
            cwd=cwd,
            env=environment,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(" ".join(arguments), 124, "", f"timed out after {timeout}s")
    except OSError as exc:
        raise ToolingError(f"Could not execute {arguments[0]}: {exc}") from exc
    return CommandResult(
        command=" ".join(arguments),
        exit_code=completed.returncode,
        stdout=completed.stdout.decode("utf-8", errors="replace"),
        stderr=completed.stderr.decode("utf-8", errors="replace"),
    )


def environment_python(root: Path) -> Path:
    if os.name == "nt":
        return root / "Scripts" / "python.exe"
    return root / "bin" / "python"


def ensure_environment(root: Path) -> Path:
    python = environment_python(root)
    if python.exists():
        return python
    result = run([sys.executable, "-m", "venv", str(root)])
    if not result.ok or not python.exists():
        raise ToolingError(f"Could not create virtual environment at {root}: {result.failure_detail()}")
    return python


def install(python: Path, packages: list[str], *, timeout: float = 900.0) -> CommandResult:
    return run(
        [str(python), "-m", "pip", "install", "--disable-pip-version-check", "--quiet", *packages],
        timeout=timeout,
    )


def ensure_ruff(root: Path) -> Path:
    python = ensure_environment(root)
    executable = python.parent / ("ruff.exe" if os.name == "nt" else "ruff")
    if executable.exists() and _installed_ruff_version(executable) == RUFF_VERSION:
        return executable
    result = install(python, [f"ruff=={RUFF_VERSION}"])
    if not result.ok or not executable.exists():
        raise ToolingError(
            f"Could not install ruff=={RUFF_VERSION} into {root}: {result.failure_detail()}"
        )
    return executable


def _installed_ruff_version(executable: Path) -> str | None:
    result = run([str(executable), "--version"])
    if not result.ok:
        return None
    parts = result.stdout.strip().split()
    return parts[1] if len(parts) >= 2 else None
