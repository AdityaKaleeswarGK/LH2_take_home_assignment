"""Multi-language containerisation, where determinism is measured, not asserted.

The acceptance bar is that the suite runs inside the container and produces the
same result twice. That is a claim about two executions, so this module makes
two executions or it does not make the claim: ``runs_identical`` is only ever
set from comparing two real runs, and a repository whose ecosystem cannot be
built here reports ``unsupported`` rather than a passing-looking default.

Everything generated lands in ``.stress_stack/container/`` — the Dockerfile, the
build log, and both run logs — so the target repository is never mutated to be
measured, and the evidence for a verdict outlives the process that produced it.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stress_stack.atomic import atomic_write_json, atomic_write_text
from stress_stack.project_detector import ProjectProfile, detect_project_profile
from stress_stack.tooling import run

VERIFIED = "verified"
UNSUPPORTED = "unsupported"
BUILD_FAILED = "build_failed"
TESTS_FAILED = "tests_failed"
NONDETERMINISTIC = "nondeterministic"

# Volatile substrings that differ between two identical runs and say nothing
# about correctness. Normalising these is what makes a comparison meaningful;
# normalising anything more would start hiding real disagreement.
_VOLATILE = (
    re.compile(r"\b\d+\.\d+s\b"),  # durations: "1.23s"
    re.compile(r"\b\d+(\.\d+)? ?ms\b"),  # durations: "12ms"
    re.compile(r"0x[0-9a-fA-F]+"),  # memory addresses
    re.compile(r"/tmp/[^\s\"']+"),  # per-run temporary paths
    re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*"),  # timestamps
)


@dataclass
class ContainerDoctorResult:
    status: str
    ecosystem: str
    dockerfile_path: Path | None
    image_tag: str
    base_image: str
    base_image_pinning: str
    build_status: str
    # None when fewer than two runs completed — the question was never answered.
    runs_identical: bool | None
    # How much the determinism comparison could actually see. `process_output`
    # only observes what the test command chose to print: a quiet runner (`go
    # test` without -v, pytest without -v) hides per-test nondeterminism, so an
    # identical verdict at this resolution is weaker than `per_test`, which
    # compares structured results for every test individually.
    determinism_resolution: str = "process_output"
    reason: str = ""
    test_runs: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ecosystem": self.ecosystem,
            "dockerfile": str(self.dockerfile_path) if self.dockerfile_path else None,
            "image": self.image_tag,
            "base_image": self.base_image,
            "base_image_pinning": self.base_image_pinning,
            "build_status": self.build_status,
            "runs_identical": self.runs_identical,
            "determinism_resolution": self.determinism_resolution,
            "reason": self.reason,
            "runs": self.test_runs,
        }


def _unsupported(ecosystem: str, reason: str) -> ContainerDoctorResult:
    return ContainerDoctorResult(
        status=UNSUPPORTED,
        ecosystem=ecosystem,
        dockerfile_path=None,
        image_tag="",
        base_image="",
        base_image_pinning="none",
        build_status="not_attempted",
        runs_identical=None,
        reason=reason,
    )


def _shell_command(command: str) -> str:
    """Embed a command in Dockerfile exec-form JSON safely.

    The command reaches us from CI workflow YAML in the target repository, so it
    is untrusted input: a quote in it would otherwise break out of the JSON
    string and produce a Dockerfile that does something other than intended.
    """
    return json.dumps(["sh", "-c", command])


def synthesize_dockerfile(profile: ProjectProfile, base_reference: str) -> str:
    """Generate Dockerfile instructions tailored to the project."""
    lang = profile.primary_language
    test_cmd = profile.default_test_command

    lines = [
        f"FROM {base_reference}",
        "WORKDIR /work",
        "ENV DEBIAN_FRONTEND=noninteractive",
        "ENV CI=true",
        # Byte-identical output across runs depends on suppressing incidental
        # nondeterminism at the source, not filtering it afterwards.
        "ENV PYTHONUNBUFFERED=1",
        "ENV PYTHONDONTWRITEBYTECODE=1",
        "ENV PYTHONHASHSEED=0",
        "ENV SOURCE_DATE_EPOCH=0",
        "ENV TZ=UTC",
        "ENV LC_ALL=C.UTF-8",
    ]

    if profile.ci_facts.system_packages:
        pkgs = " ".join(sorted(set(profile.ci_facts.system_packages)))
        lines.append(
            "RUN apt-get update && apt-get install -y --no-install-recommends "
            f"{pkgs} && rm -rf /var/lib/apt/lists/*"
        )

    if lang == "python":
        lines += [
            "COPY . /work",
            "RUN pip install --no-cache-dir pytest",
            "RUN if [ -f pyproject.toml ] || [ -f setup.py ]; "
            "then pip install --no-cache-dir -e .; fi",
        ]
    elif lang == "rust":
        lines += ["COPY . /work", "RUN cargo build --tests --offline || cargo build --tests"]
    elif lang in {"typescript", "javascript"}:
        tool = profile.toolchain
        # `npm ci` installs strictly from the lockfile; `install` may resolve
        # differently between runs, which is the opposite of what we need.
        install = "npm ci" if tool == "npm" else f"{tool} install --frozen-lockfile"
        lines += ["COPY . /work", f"RUN if [ -f package.json ]; then {install}; fi"]
        if profile.pre_build_command:
            lines.append(f"RUN {profile.pre_build_command}")
    elif lang == "go":
        lines += ["COPY . /work", "RUN if [ -f go.mod ]; then go mod download; fi"]
    elif lang == "cpp":
        lines += [
            "RUN apt-get update && apt-get install -y --no-install-recommends "
            "build-essential cmake ninja-build && rm -rf /var/lib/apt/lists/*",
            "COPY . /work",
        ]
        if profile.pre_build_command:
            lines.append(f"RUN {profile.pre_build_command}")
    else:
        lines.append("COPY . /work")

    lines.append(f"CMD {_shell_command(test_cmd)}")
    return "\n".join(lines) + "\n"


def _normalize(text: str) -> str:
    for pattern in _VOLATILE:
        text = pattern.sub("<volatile>", text)
    return text.strip()


def _run_suite(image: str, name: str) -> dict[str, Any]:
    """Run the container's own CMD once, under the same lockdown as validate."""
    started = time.monotonic()
    result = run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            image,
        ],
        timeout=1800.0,
    )
    output = result.stdout + result.stderr
    return {
        "name": name,
        "exit_code": result.exit_code,
        "seconds": round(time.monotonic() - started, 2),
        "normalized_output": _normalize(output),
        "log_tail": "\n".join(output.strip().splitlines()[-20:]),
    }


def run_container_verification(
    repo_root: Path | str, profile: ProjectProfile | None = None
) -> ContainerDoctorResult:
    """Build the image and run the suite twice, comparing the two runs."""
    root = Path(repo_root)
    prof = profile or detect_project_profile(root)

    # Python keeps the verified path: it compares against a host baseline and
    # parses JUnit per test, which is strictly stronger than output comparison.
    if prof.primary_language == "python":
        from stress_stack.graph import build_container_artifacts

        legacy = build_container_artifacts(str(root))
        return ContainerDoctorResult(
            status=legacy.status,
            ecosystem="python",
            dockerfile_path=legacy.dockerfile,
            image_tag=legacy.image,
            base_image=legacy.base_image,
            base_image_pinning="delegated_to_container_stage",
            build_status=legacy.build_status,
            runs_identical=legacy.identical,
            # The Python path parses JUnit and compares every test individually,
            # and additionally compares against the host baseline.
            determinism_resolution="per_test",
            reason=legacy.baseline_match,
            test_runs=legacy.runs,
        )

    if not shutil.which("docker"):
        return _unsupported(prof.primary_language, "docker_not_installed")

    evidence = root / ".stress_stack" / "container"
    evidence.mkdir(parents=True, exist_ok=True)

    base_reference, pinning = pin_base_image(prof.base_image)
    dockerfile_path = evidence / "Dockerfile"
    atomic_write_text(dockerfile_path, synthesize_dockerfile(prof, base_reference))

    # Keep the build context small and stable; without this the context carries
    # .git/ and .stress_stack/ and differs between runs.
    from stress_stack.container import _DOCKERIGNORE

    atomic_write_text(evidence / "Dockerfile.dockerignore", _DOCKERIGNORE)

    tag = f"stress-stack/{root.name.lower()}:verify"
    build = run(
        ["docker", "build", "-t", tag, "-f", str(dockerfile_path), str(root)],
        timeout=3600.0,
    )
    atomic_write_text(evidence / "build.log", build.stdout + build.stderr)

    if not build.ok:
        result = ContainerDoctorResult(
            status=BUILD_FAILED,
            ecosystem=prof.primary_language,
            dockerfile_path=dockerfile_path,
            image_tag=tag,
            base_image=base_reference,
            base_image_pinning=pinning,
            build_status="failed",
            runs_identical=None,
            reason="\n".join((build.stdout + build.stderr).strip().splitlines()[-10:]),
        )
        atomic_write_json(evidence / "doctor.json", result.to_dict())
        return result

    first = _run_suite(tag, "run-1")
    second = _run_suite(tag, "run-2")
    identical = (
        first["exit_code"] == second["exit_code"]
        and first["normalized_output"] == second["normalized_output"]
    )
    passed = first["exit_code"] == 0 and second["exit_code"] == 0

    if not identical:
        status, reason = NONDETERMINISTIC, "two runs disagreed"
    elif not passed:
        status, reason = TESTS_FAILED, f"suite exited {first['exit_code']}"
    else:
        status, reason = VERIFIED, ""

    result = ContainerDoctorResult(
        status=status,
        ecosystem=prof.primary_language,
        dockerfile_path=dockerfile_path,
        image_tag=tag,
        base_image=base_reference,
        base_image_pinning=pinning,
        build_status="verified",
        runs_identical=identical,
        reason=reason,
        # The normalized output is large and only needed for the comparison
        # itself; the tail is what a human reads.
        test_runs=[
            {k: v for k, v in single.items() if k != "normalized_output"}
            for single in (first, second)
        ],
    )
    atomic_write_json(evidence / "doctor.json", result.to_dict())
    return result


def pin_base_image(tag: str) -> tuple[str, str]:
    """Resolve the ecosystem's base tag to a digest, reusing the proven logic."""
    from stress_stack.container import pin_image_by_digest

    return pin_image_by_digest(tag)
