from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from stress_stack.errors import ToolingError

RUFF_VERSION = "0.15.7"


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
) -> CommandResult:
    environment = os.environ.copy()
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
