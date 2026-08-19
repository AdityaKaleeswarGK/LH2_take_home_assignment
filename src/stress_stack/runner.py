"""Execute a task's verifier and return a parsed report.

The gates in :mod:`stress_stack.verification` decide what a run *means*; this
decides how a run *happens*. Keeping them apart is what lets the same verdict be
reached in a container on a machine that has Docker and in a virtual environment
on one that does not, with the backend recorded rather than assumed.

Every run happens in a container. There is no host fallback: an isolated run and
an unisolated one do not support the same claim, and quietly substituting the
weaker one would make "reproduces on our machine" an assertion rather than a
property.

Two further properties matter:

* **A run that broke is not a run that failed.** pytest exits 1 for test
  failures and 5 for an empty collection; both are results. Anything else — a
  timeout, an OOM kill, a container that would not start — is the harness
  failing, and recording it as a task verdict would manufacture evidence.
* **Node ids are translated against the tree, not guessed.** JUnit reports a
  dotted ``classname``; pytest's command line wants a path. Which prefix of the
  dotted name is the module is answered by looking for the file.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from stress_stack.errors import ToolingError
from stress_stack.sandbox import SandboxPolicy, pytest_command, run_sandboxed
from stress_stack.tooling import run
from stress_stack.verification import RunReport, parse_report

DOCKER = "docker"


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """One execution of a verifier, and whether it is usable as evidence."""

    name: str
    report: RunReport
    exit_code: int | None
    seconds: float
    backend: str
    infrastructure_failure: str | None
    detail: str = ""
    expect_empty: bool = False
    """Set when collecting nothing is a legitimate outcome, never for a suite."""

    @property
    def usable(self) -> bool:
        """A run is evidence only if it actually ran something.

        Exit code alone cannot tell "tests failed" from "the interpreter could
        not start pytest" — both are 1. click's image was built without a test
        runner, every run exited 1 having collected nothing, and because that
        looked like a result the screen concluded that none of forty-two
        candidates had a behavioural test change. A misleading verdict is worse
        than a crash, so an empty full-suite run is infrastructure.
        """
        if self.infrastructure_failure is not None:
            return False
        return bool(self.report.results) or self.expect_empty

    @property
    def empty(self) -> bool:
        return not self.report.results

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "backend": self.backend,
            "exit_code": self.exit_code,
            "seconds": round(self.seconds, 2),
            "infrastructure_failure": self.infrastructure_failure
            or (None if self.usable else "collected_no_tests"),
            "detail": self.detail[:400],
            "total": len(self.report.results),
            "passing": len(self.report.passing()),
            "failing": len(self.report.failing()),
            "class_counts": self.report.class_counts(),
        }


class Runner(Protocol):
    backend: str

    def execute(
        self, tree: Path, evidence: Path, name: str, targets: list[str] | None = None
    ) -> RunOutcome: ...

    def selection_id(self, test_id: str) -> str:
        """The id a targeted re-run of this runner can select on."""
        ...


def pytest_argument(tree: Path, node_id: str) -> str:
    """Normalise any test id into the path form pytest accepts on its command line.

    Two namespaces reach this function and they do not look alike. JUnit writes
    a dotted ``classname`` — ``glom.test.test_basic.TestSpec::test_call`` — with
    no marker saying where the module stops and the class begins; the filesystem
    supplies it, since the longest dotted prefix naming a real file is the
    module. coverage contexts arrive already path-addressed, and splitting those
    on ``.`` shears the ``.py`` off into a phantom class.
    """
    if "::" not in node_id:
        return node_id
    head = node_id.split("::", 1)[0]
    if head.endswith(".py") or "/" in head:
        return node_id

    classname, _, name = node_id.rpartition("::")
    parts = [part for part in classname.split(".") if part]
    for cut in range(len(parts), 0, -1):
        candidate = Path(*parts[:cut]).with_suffix(".py")
        if (tree / candidate).is_file():
            return "::".join([candidate.as_posix(), *parts[cut:], name])
    return node_id


def pytest_arguments(tree: Path, targets: list[str] | None) -> list[str]:
    return [pytest_argument(tree, target) for target in targets or []]


def forget_source_roots() -> None:
    """Invalidate the cache after a tree has been rewritten in place.

    Only ``build_evaluation_tree`` rewrites a tree that has already been run
    against, so it is the only caller. Clearing every entry rather than one
    keeps this correct under the validate pool, where several candidates hold
    entries at once: the cost of another candidate recomputing its roots is a
    directory walk, and the cost of getting this wrong is a task measured
    against the wrong copy of its own package.
    """
    _cached_source_roots.cache_clear()


@lru_cache(maxsize=64)
def _cached_source_roots(resolved_tree: str, mount: str | None) -> str:
    return _compute_source_roots(Path(resolved_tree), mount)


def source_roots(tree: Path, *, mount: str | None = "/work") -> str:
    """The PYTHONPATH under which a tree's own packages win.

    ``PYTHONPATH=/work`` assumes the packages sit directly under the repository
    root. Under a ``src/`` layout they do not, so ``/work`` contains no
    importable package, ``import click`` falls through to the copy the image
    baked in at build time, and every run measures frozen code instead of the
    task — silently, and with entirely plausible-looking results. click's
    excision tasks all reported that removing a function body changed no test
    verdict, which was true of the installed copy and irrelevant to the task.

    The roots are derived with the same ``package_root`` the graph uses to name
    modules, so a layout this tool can parse is a layout it can also run.

    ``mount`` names where the tree appears to the process being launched. The
    default is the container's read-only mount; passing ``None`` asks for host
    absolute paths instead, which is what the coverage and hygiene runs need.
    Both callers want the same answer to the same question, and keeping two
    derivations meant the host ones measured the installed copy while the
    container ones measured the source tree.

    Cached per resolved tree: a candidate runs eight times against at most four
    trees, and each call walks the whole tree. See ``forget_source_roots`` for
    the one case where a tree changes underneath the cache.
    """
    return _cached_source_roots(str(tree.resolve()), mount)


def _compute_source_roots(tree: Path, mount: str | None) -> str:
    from stress_stack.symbols import discover_python_files, package_root

    directories: set[Path] = set()
    for path in discover_python_files(tree):
        directories.add(package_root(tree, path))

    # A directory earns a place on the path only if a package actually lives
    # directly under it. Without that test every example script's own folder
    # qualifies — click contributes eleven — which is noise at best and a
    # shadowing hazard at worst.
    roots: set[str] = set()
    for directory in directories:
        if directory == tree:
            continue
        if not any(child.is_dir() and (child / "__init__.py").is_file()
                   for child in directory.iterdir()):
            continue
        try:
            relative = directory.relative_to(tree)
        except ValueError:
            continue
        roots.add(str(directory) if mount is None else f"{mount}/{relative.as_posix()}")
    # The tree itself stays last so a src-layout package outranks anything
    # sitting at the repository root, while a flat layout still resolves.
    base = str(tree) if mount is None else mount
    separator = os.pathsep if mount is None else ":"
    return separator.join([*sorted(roots), base])


@dataclass
class DockerRunner:
    """Run the suite in a throwaway container with no network and a read-only tree."""

    image: str
    policy: SandboxPolicy = field(default_factory=SandboxPolicy)
    backend: str = DOCKER

    def selection_id(self, test_id: str) -> str:
        """pytest can select every id it reports, parametrised cases included."""
        return test_id

    def execute(
        self, tree: Path, evidence: Path, name: str, targets: list[str] | None = None
    ) -> RunOutcome:
        evidence.mkdir(parents=True, exist_ok=True)
        # A failed container launch must not inherit a successful report from a
        # previous run with the same name.
        (evidence / f"{name}.xml").unlink(missing_ok=True)
        started = time.monotonic()
        result = run_sandboxed(
            self.image,
            pytest_command(name, pytest_arguments(tree, targets)),
            code_dir=tree,
            evidence_dir=evidence,
            policy=self.policy,
            environment={"PYTHONPATH": source_roots(tree)},
        )
        seconds = time.monotonic() - started
        return RunOutcome(
            name=name,
            report=parse_report(evidence / f"{name}.xml"),
            exit_code=result.exit_code,
            seconds=seconds,
            backend=self.backend,
            infrastructure_failure=result.infrastructure_failure,
            detail=(result.stderr.strip() or result.stdout.strip())[-400:]
            if result.infrastructure_failure
            else "",
        )


def docker_available() -> bool:
    """Whether a daemon is actually reachable, not merely whether the CLI exists."""
    try:
        return run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=20.0).ok
    except Exception:
        return False


def image_available(image: str) -> bool:
    return run(["docker", "image", "inspect", image], timeout=30.0).ok


@dataclass
class LanguageRunner:
    """Run an ecosystem's own test command in the same sandbox pytest gets.

    Structurally identical to ``DockerRunner`` — same image, same policy, same
    isolation — and different in only two respects, both forced by the
    ecosystem rather than chosen: the command comes from the language's test
    plan, and results are read from the runner's stdout because `go test` and
    `cargo test` have no JUnit file to write.

    The raw output is still written to the evidence directory. A verdict whose
    supporting output was thrown away is not reproducible, and every gate
    verdict here has to be re-checkable from disk.
    """

    image: str
    plan: Any
    policy: SandboxPolicy = field(default_factory=SandboxPolicy)
    backend: str = DOCKER

    def selection_id(self, test_id: str) -> str:
        return self.plan.selection_id(test_id)

    def environment(self) -> dict[str, str]:
        """Where this toolchain may write, given a read-only code mount.

        The mount is read-only on purpose — a run must not mutate the snapshot
        it is being judged against — and compiled ecosystems have to put build
        output somewhere. Go already worked by accident: `GOCACHE` defaults
        under `HOME`, which the sandbox sets to the writable `/tmp`. Cargo does
        not, and insists on `<workspace>/target`, so every Rust run died before
        a test with `failed to open: /work/target/debug/.cargo-lock: Read-only
        file system`.

        Redirecting to `/tmp` keeps the guarantee rather than relaxing it: the
        code mount stays read-only, and the only writable space is the run's own
        throwaway tmpfs. No PYTHONPATH is set for any of these — it is
        meaningless in a Go or Rust image and is the shadowing vector that broke
        the repositories pytest itself depends on.
        """
        if self.plan.language == "rust":
            # Only the *target* directory. `CARGO_HOME` holds the registry the
            # image build already populated, and the run has no network — so
            # redirecting it to an empty tmpfs makes every dependency
            # unresolvable. The rule this follows: redirect what a run must
            # write, never what it must read.
            return {"CARGO_TARGET_DIR": "/tmp/target"}
        # Go needs nothing. `GOCACHE` and `GOMODCACHE` already default under
        # `HOME`, which the sandbox sets to the writable `/tmp`, and the image
        # build downloaded the module cache there. Overriding them looked
        # symmetrical with Rust and broke every Go run with `dial tcp: lookup
        # proxy.golang.org: network is unreachable` — the modules were fine, and
        # pointing at a fresh tmpfs was the whole problem.
        return {}

    def execute(
        self, tree: Path, evidence: Path, name: str, targets: list[str] | None = None
    ) -> RunOutcome:
        evidence.mkdir(parents=True, exist_ok=True)
        log_path = evidence / f"{name}.log"
        log_path.unlink(missing_ok=True)
        started = time.monotonic()
        result = run_sandboxed(
            self.image,
            self.plan.suite_command(targets),
            code_dir=tree,
            evidence_dir=evidence,
            policy=self.policy,
            environment=self.environment(),
            result_exit_codes=getattr(self.plan, "result_exit_codes", None),
        )
        seconds = time.monotonic() - started
        log_path.write_text(
            f"$ {' '.join(self.plan.suite_command(targets))}\n\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}\n",
            encoding="utf-8",
        )
        report = self.plan.parse(result.stdout, result.stderr, result.exit_code)
        return RunOutcome(
            name=name,
            report=report,
            exit_code=result.exit_code,
            seconds=seconds,
            backend=self.backend,
            infrastructure_failure=result.infrastructure_failure,
            detail=(result.stderr.strip() or result.stdout.strip())[-400:]
            if result.infrastructure_failure
            else "",
        )


def select_runner(*, image: str, language: str = "python") -> Any:
    """The runner for this ecosystem, or an error explaining why there is none."""
    if language != "python":
        from stress_stack.test_runners import plan_for, supported_languages

        plan = plan_for(language)
        if plan is None:
            raise ToolingError(
                f"Validation has no test plan for {language!r}. Supported: "
                f"{', '.join(sorted(supported_languages())) or 'python only'}. "
                "Gate verdicts are only produced from parsed per-test results, "
                "never inferred from an exit code."
            )
        _require_container(image)
        # Go, Rust and C++ compile a test binary and exec it; the default
        # `noexec` tmpfs blocks that before any test runs.
        compiled = language in {"go", "rust", "c", "cpp"}
        return LanguageRunner(
            image=image, plan=plan, policy=SandboxPolicy(allow_tmp_exec=compiled)
        )
    _require_container(image)
    return DockerRunner(image=image)


def _require_container(image: str) -> None:
    """The container is the only backend, and its absence is an error.

    There was a host-interpreter fallback here. It is gone deliberately: a
    verdict reached without network isolation, a read-only tree and a resource
    ceiling is a weaker claim than the one the brief asks for, and a pipeline
    that silently produces the weaker claim is worse than one that stops. If the
    daemon is down, the answer is to start it, not to accept softer evidence.
    """
    if not docker_available():
        raise ToolingError(
            "Docker daemon is not reachable. Validation runs only in a container, "
            "so start Docker and re-run."
        )
    if not image_available(image):
        raise ToolingError(
            f"Container image {image!r} does not exist. Run `stress-stack container` "
            "first — it builds the image validation runs in."
        )
