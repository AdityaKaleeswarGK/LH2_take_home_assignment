"""Run a repository's test suite as untrusted code.

Executing a repository's tests *is* arbitrary code execution: ``conftest.py``,
package imports, and build hooks all run before a single assertion is checked.
Doing that on the host exposes credentials, the SSH agent, the Docker socket,
the home directory, and cloud metadata endpoints. Every run therefore happens
in a throwaway container with:

* **no network** — nothing can phone home, and no test can silently depend on
  the internet, which also removes a source of nondeterminism;
* **a read-only root filesystem**, with writable space confined to a ``tmpfs``
  and the evidence mount;
* **the task tree mounted read-only**, so a run cannot mutate the snapshot it
  is being judged against;
* **all capabilities dropped** and ``no-new-privileges`` set;
* **CPU, memory, PID and wall-clock ceilings**, so a runaway test is bounded;
* **a sanitised environment** — containers inherit nothing from the host, and
  only the variables named here are passed in;
* **argument arrays, never shell strings**, so repository-controlled text
  cannot be interpolated into a command.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stress_stack.tooling import run


def _container_slots() -> int:
    """How many sandboxed runs may execute at once, machine-wide.

    Two levels of concurrency meet here: the validate stage puts several
    candidates through the gates at once, and each candidate runs its two
    verifier lanes together. Neither knows about the other, so the bound lives
    at the one point every run passes through rather than being split between
    them — otherwise the two multiply.

    The unit is cores, not runs: each container is capped at
    ``SandboxPolicy.cpus``, so one run per two cores keeps a full pool from
    oversubscribing the machine it is measuring on.
    """
    override = os.environ.get("STRESS_STACK_CONTAINER_SLOTS")
    if override and override.isdigit() and int(override) > 0:
        return int(override)
    return max(1, (os.cpu_count() or 2) // 2)


_CONTAINER_SLOTS = threading.BoundedSemaphore(_container_slots())

_DEFAULT_ENVIRONMENT = {
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "PYTHONHASHSEED": "0",
    "LC_ALL": "C.UTF-8",
    "LANG": "C.UTF-8",
    "TZ": "UTC",
    "HOME": "/tmp",
}


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    network: str = "none"
    cpus: str = "2"
    memory: str = "2g"
    pids: int = 512
    tmpfs_size: str = "512m"
    timeout_seconds: float = 900.0
    read_only_root: bool = True
    drop_capabilities: bool = True
    run_as_host_user: bool = True
    # Compiled ecosystems build a test binary into /tmp and then execute it, so
    # a `noexec` tmpfs stops them before a single test runs — `go test` reports
    # `fork/exec /tmp/go-build.../pkg.test: permission denied`. Interpreted
    # ecosystems never need this, so it stays off unless a runner asks.
    #
    # The relaxation is narrow: the code mount is still read-only, the root
    # filesystem is still read-only, the network is still off and every
    # capability is still dropped. The only new power is executing a file the
    # run itself just compiled inside its own throwaway tmpfs.
    allow_tmp_exec: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "network": self.network,
            "cpus": self.cpus,
            "memory": self.memory,
            "pids": self.pids,
            "tmpfs_size": self.tmpfs_size,
            "timeout_seconds": self.timeout_seconds,
            "read_only_root": self.read_only_root,
            "drop_capabilities": self.drop_capabilities,
            "allow_tmp_exec": self.allow_tmp_exec,
        }


@dataclass(frozen=True, slots=True)
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    out_of_memory: bool
    command: list[str] = field(default_factory=list)

    @property
    def infrastructure_failure(self) -> str | None:
        """Distinguish a broken run from a failing test suite.

        pytest exits 1 for test failures and 5 for no-tests-collected; both are
        results. Anything else here is the harness failing, and must not be
        recorded as a task verdict.
        """
        if self.timed_out:
            return "timeout"
        if self.out_of_memory:
            return "out_of_memory"
        if self.exit_code in {0, 1, 5}:
            return None
        return f"container_exit_{self.exit_code}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "out_of_memory": self.out_of_memory,
            "infrastructure_failure": self.infrastructure_failure,
            "stdout_tail": "\n".join(self.stdout.strip().splitlines()[-8:]),
            "stderr_tail": "\n".join(self.stderr.strip().splitlines()[-8:]),
        }


def build_arguments(
    image: str,
    command: list[str],
    *,
    code_dir: Path,
    evidence_dir: Path,
    policy: SandboxPolicy,
    environment: dict[str, str] | None = None,
) -> list[str]:
    """Assemble the ``docker run`` argument vector for one isolated run."""
    arguments = [
        "docker",
        "run",
        "--rm",
        f"--network={policy.network}",
        f"--cpus={policy.cpus}",
        f"--memory={policy.memory}",
        f"--pids-limit={policy.pids}",
        "--security-opt=no-new-privileges",
        f"--tmpfs=/tmp:rw{',exec' if policy.allow_tmp_exec else ''}"
        f",size={policy.tmpfs_size},mode=1777",
        f"--volume={_resolve(code_dir)}:/work:ro",
        f"--volume={_resolve(evidence_dir)}:/evidence:rw",
        "--workdir=/work",
    ]
    if policy.read_only_root:
        arguments.append("--read-only")
    if policy.drop_capabilities:
        arguments.append("--cap-drop=ALL")
    if policy.run_as_host_user and os.name != "nt":
        arguments.append(f"--user={os.getuid()}:{os.getgid()}")

    # `None` means "no opinion", and gets the flat-layout default. An explicit
    # mapping — including an empty one — means the caller has decided, and a
    # PYTHONPATH it did not ask for must not be reinstated. LanguageRunner
    # passes `{}` precisely to keep a Python variable out of a Go or Rust image.
    merged = dict(_DEFAULT_ENVIRONMENT)
    if environment is None:
        merged["PYTHONPATH"] = "/work"
    else:
        merged.update(environment)
    for key, value in sorted(merged.items()):
        arguments.append(f"--env={key}={value}")

    arguments.append(image)
    arguments.extend(command)
    return arguments


def run_sandboxed(
    image: str,
    command: list[str],
    *,
    code_dir: Path,
    evidence_dir: Path,
    policy: SandboxPolicy | None = None,
    environment: dict[str, str] | None = None,
) -> SandboxResult:
    policy = policy or SandboxPolicy()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    arguments = build_arguments(
        image,
        command,
        code_dir=code_dir,
        evidence_dir=evidence_dir,
        policy=policy,
        environment=environment,
    )
    with _CONTAINER_SLOTS:
        result = run(arguments, timeout=policy.timeout_seconds)
    combined = result.stdout + result.stderr
    return SandboxResult(
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        timed_out=result.exit_code == 124,
        out_of_memory="Killed" in combined or result.exit_code == 137,
        command=arguments,
    )


def pytest_command(report_name: str, targets: list[str] | None = None) -> list[str]:
    """The verifier invocation, with tracebacks retained for failure classification.

    ``--continue-on-collection-errors`` is what makes the pre-change run usable
    at all. A feature's new test file imports the symbol the feature adds, so it
    cannot be collected before the change; without this flag that one file
    aborts the entire run and eighty-six other tests report nothing, which
    destroys the collateral baseline along with the fail-before evidence. With
    it, the uncollectable file is recorded as an error and everything else still
    produces a verdict.
    """
    command = [
        "python",
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "--continue-on-collection-errors",
        "--tb=short",
        "-q",
        f"--junit-xml=/evidence/{report_name}.xml",
    ]
    if targets:
        command.extend(targets)
    return command


def _resolve(path: Path) -> str:
    """Absolute, symlink-free path — a mount source must not be redirectable."""
    return str(path.resolve(strict=True))
