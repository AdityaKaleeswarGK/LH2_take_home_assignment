"""Choose the environment a *candidate* is judged in, rather than reusing HEAD's.

Validation used one image for every candidate: the tag the ``container`` stage
built, from HEAD's declared toolchain and HEAD's lock. That is wrong in two ways
which turn out to be the same way.

**A repository the test runner depends on cannot be validated at all.** Task
trees are mounted at ``/work`` and put on the interpreter's path ahead of the
installed packages, which is what makes a run measure the task instead of the
copy baked into the image. When the repository *is* ``pluggy``, that same
mechanism replaces the copy pytest itself is running on, and a modern pytest
cannot start against a 2018 pluggy. On pluggy this rejected sixteen of
thirty-five candidates and the run shipped seven tasks instead of ten. No path
trick fixes it: pytest and the tree need the same copy, so the runner has to be
the one that moves.

**Historical trees are judged under HEAD's toolchain.** A 2014 pull request run
under a 2025 interpreter fails its own suite, which the gates catch correctly
and expensively.

Both are the same question — *what runtime does this particular tree need?* — so
they get one answer, and it is read out of the tree by an agent rather than
encoded here. This module used to answer it with a table of per-ecosystem
resolvers, about two hundred and fifty lines of manifest readers and CI parsers
that generalised no further than the four ecosystems someone had written a
branch for. Deleting them is the point: a fifth ecosystem is now free, and the
agent already finds things those readers could not — on pluggy 0.7.1 it installs
``pytest-benchmark`` because a test file uses the fixture, which no manifest in
that revision declares.

What stays mechanical, and why:

* **The shadow.** Whether the mounted tree will replace part of the test runner
  is a fact about this harness's mount, not about the repository, so it is
  computed here and handed to the agent as a premise.
* **The checking.** Nothing the agent proposes reaches a shell unvalidated, and a
  test command that narrows collection is refused outright — see
  :mod:`stress_stack.environment_agent`.
* **The verdicts.** No model decides whether a test passed or a task ships.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stress_stack.atomic import atomic_write_json
from stress_stack.errors import StressStackError
from stress_stack.tooling import run

# Packages the Python test runner itself imports. A repository that *is* one of
# these cannot be verified against a runner built for a different version of it.
RUNNER_DEPENDENCIES = frozenset(
    {"pluggy", "iniconfig", "packaging", "exceptiongroup", "tomli", "pytest", "_pytest"}
)

# Not a budget. Past this the resolution is wrong rather than merely
# expensive: a repository whose every commit resolves differently has no
# eras at all, and building sixty-five images would not fix that.
_SAFETY_CEILING = 64

# Top-level import names whose distribution is not the same string.
_DISTRIBUTION_OF = {"_pytest": "pytest"}



@dataclass(frozen=True, slots=True)
class Era:
    """A set of commits that can share one environment.

    Keyed by the nearest release tag reachable from the tree, because that is
    the boundary the environment actually turns on: within a release window a
    project declares the same dependencies and the same toolchain, and — for a
    repository that is itself a dependency of the test runner — publishes the
    same version. Excision candidates come from HEAD and share its era.

    The key doubles as the version hint, since ``git describe`` answers both
    questions at once.
    """

    key: str
    version_hint: str | None = None


def plan_eras(repository: Any, candidates: list[Any]) -> dict[str, Era]:
    """Group the ranked pool into eras *before* anything is staged.

    One `git describe` per distinct base commit — no containers, no trees
    materialised, no resolution. Doing this up front is what removes the image
    budget: the number of environments a pool needs is a fact about the pool,
    and lazily resolving one candidate at a time meant nothing could know it
    until it was too late to plan for.
    """
    described: dict[str, str] = {}
    eras: dict[str, Era] = {}
    for candidate in candidates:
        revision = str((candidate.signals or {}).get("base_sha") or "")
        if not revision:
            # Excision candidates are cut from HEAD, which is the era the
            # container stage already built and proved.
            eras[candidate.candidate_id] = Era(key="HEAD")
            continue
        if revision not in described:
            try:
                tag = repository.run(
                    ["describe", "--tags", "--abbrev=0", revision], record=False
                ).strip()
            except Exception:  # noqa: BLE001 — an undescribable commit is its own era
                tag = ""
            described[revision] = tag.lstrip("v") or revision[:12]
        key = described[revision]
        eras[candidate.candidate_id] = Era(key=key, version_hint=key or None)
    return eras


def era_count(eras: dict[str, Era]) -> int:
    return len({era.key for era in eras.values()})


@dataclass(frozen=True, slots=True)
class RuntimeSpec:
    """An image a candidate can be judged in, and what was read to decide it.

    ``base_image`` is an unresolved registry tag; the caller pins it by digest.
    ``install`` are shell commands run at build time, when the network is still
    available — at run time it is not. ``evidence`` is the audit trail: every
    entry names the file the value came from, so a surprising image can be
    traced to the line that asked for it.
    """

    language: str
    base_image: str
    install: tuple[str, ...] = ()
    system_packages: tuple[str, ...] = ()
    shadowed: tuple[str, ...] = ()
    test_command: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        material = json.dumps(
            {
                "language": self.language,
                "base_image": self.base_image,
                "install": list(self.install),
                "system_packages": sorted(self.system_packages),
                "test_command": list(self.test_command),
            },
            sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]

    def tag(self, repository_name: str) -> str:
        stem = re.sub(r"[^a-z0-9]+", "-", self.base_image.split("@")[0].lower()).strip("-")
        return f"stress-stack/{repository_name.lower()}:{stem[:24]}-{self.fingerprint[:8]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "base_image": self.base_image,
            "install": list(self.install),
            "system_packages": list(self.system_packages),
            "shadowed": list(self.shadowed),
            "test_command": list(self.test_command),
            "evidence": self.evidence,
            "fingerprint": self.fingerprint,
        }


# --------------------------------------------------------------------------
# Reading a tree
# --------------------------------------------------------------------------


def shadowed_distributions(tree: Path) -> tuple[str, ...]:
    """Runner dependencies this tree provides its own copy of.

    Derived from the same ``source_roots`` the run itself will use, so the answer
    is about what will actually be importable rather than what the layout looks
    like. Kept mechanical rather than asked of the agent: it is a fact about the
    harness's mount, not about the repository, and the agent is told it.
    """
    from stress_stack.runner import source_roots

    found: set[str] = set()
    for entry in source_roots(tree, mount=None).split(os.pathsep):
        directory = Path(entry)
        if not directory.is_dir():
            continue
        for child in directory.iterdir():
            name = child.name.removesuffix(".py")
            if name not in RUNNER_DEPENDENCIES:
                continue
            if child.is_dir() and not (child / "__init__.py").is_file():
                continue
            if not child.is_dir() and child.suffix != ".py":
                continue
            found.add(_DISTRIBUTION_OF.get(name, name))
    return tuple(sorted(found))


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def resolve_runtime(
    tree: Path,
    language: str,
    head: "HeadRuntime",
    *,
    client: Any,
    era: "Era | None" = None,
) -> RuntimeSpec | None:
    """What runtime this tree needs, read out of the tree by an agent.

    This replaced about two hundred and fifty lines of per-ecosystem branching —
    manifest readers, CI-matrix parsers, a table of resolvers — none of which
    generalised past the four ecosystems someone had written a branch for. The
    agent reads the same files and reaches the same kind of answer without the
    table, which is what makes a fifth ecosystem free.

    It also reaches better answers. On pluggy 0.7.1 it installed
    ``pytest-benchmark`` because a test file uses the ``benchmark`` fixture — a
    dependency no manifest in that revision declares, and one no reader written
    against manifests could have found.

    HEAD's era is exempt: the container stage already built and proved an image
    for it, carrying the real hash-pinned lock that a synthesized environment
    cannot.
    """
    if era is not None and era.key == "HEAD":
        return None

    from stress_stack.environment_agent import propose_environment

    proposal = propose_environment(
        client,
        tree,
        language=language,
        era_key=era.key if era else "unknown",
        shadowed=shadowed_distributions(tree) if language == "python" else (),
        version_hint=era.version_hint if era else None,
    )
    if not proposal.usable:
        raise EnvironmentProposalError(
            f"the environment proposed for era {era.key if era else '?'} did not pass "
            f"checking: {'; '.join(proposal.rejections) or 'no answer'}"
        )
    return RuntimeSpec(
        language=language,
        base_image=proposal.base_image,
        install=proposal.install,
        system_packages=proposal.system_packages,
        shadowed=shadowed_distributions(tree) if language == "python" else (),
        test_command=proposal.test_command,
        evidence={
            "agent": {
                "turns": proposal.turns,
                "tool_calls": proposal.tool_calls,
                "cited": proposal.evidence,
                "refused_generated": list(proposal.refused_generated),
            }
        },
    )


class EnvironmentProposalError(StressStackError):
    """The agent's answer did not survive checking, and there is no second guess."""


# --------------------------------------------------------------------------
# Building and reusing the images
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HeadRuntime:
    """What the container stage already built, and what a resolver compares to."""

    image: str
    language: str
    toolchain_version: str


def render_dockerfile(spec: RuntimeSpec, base_image: str) -> str:
    """A runner image, not a copy of the repository.

    There is no ``COPY`` here on purpose: validation mounts the tree read-only at
    ``/work``, so the image only has to supply a toolchain, a compatible runner
    and enough third-party packages for the suite to load. That is why a
    per-candidate image is affordable at all — base layers are shared across
    every key, and only the install layer differs.
    """
    packages = " ".join(["git", "less", *spec.system_packages])
    lines = [
        f"# Generated by stress-stack for one candidate's runtime ({spec.language}).",
        f"FROM {base_image}",
        "",
        "ENV LC_ALL=C.UTF-8 LANG=C.UTF-8",
    ]
    if spec.language == "python":
        lines += [
            "ENV PYTHONDONTWRITEBYTECODE=1 \\",
            "    PYTHONUNBUFFERED=1 \\",
            "    PYTHONHASHSEED=0 \\",
            "    PIP_NO_CACHE_DIR=1 \\",
            "    PIP_DISABLE_PIP_VERSION_CHECK=1",
        ]
    lines += [
        "",
        "WORKDIR /work",
        "",
        f"RUN apt-get update && apt-get install -y --no-install-recommends {packages} \\",
        "    && rm -rf /var/lib/apt/lists/*",
        "",
    ]
    if spec.shadowed:
        lines += [
            "# This repository provides its own copy of a package the test runner",
            "# imports. Its version is pinned and the runner is not, so the",
            "# resolver picks a runner that accepts it rather than the reverse.",
        ]
    for command in spec.install:
        lines += [f"RUN {command}", ""]
    lines += [f"CMD {list(spec.test_command)}", ""]
    return "\n".join(lines)


@dataclass
class RuntimeImages:
    """Builds and reuses one image per distinct runtime, under a budget.

    There is no budget to choose. ``expected_eras`` comes from ``plan_eras``,
    which counts what the ranked pool actually needs before anything is staged,
    so the ceiling is a measurement rather than a policy. Two guesses were made
    here before that — four, then twelve — and the first sent fourteen pluggy
    candidates back to an image that could not run them.

    ``_SAFETY_CEILING`` is what remains, and it is not a number about
    environments: it is the point past which the resolution is telling us it is
    wrong, because a repository whose every commit resolves differently has no
    eras at all. Reaching it falls back to the HEAD image and records that it
    did, so a degraded verdict is never silent.
    """

    repository_name: str
    head: HeadRuntime
    # Required. There is no deterministic second path: an environment nobody
    # can work out is a run that should stop, not one that quietly uses the
    # wrong image and reports verdicts from it.
    client: Any = None
    stamp_path: Path | None = None
    expected_eras: int = 1
    built: dict[str, str] = field(default_factory=dict)
    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Resolution is per era, not per candidate: every candidate in a release
    # window reads the same manifests and reaches the same answer.
    by_era: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)

    def runtime_for(
        self, tree: Path, *, era: Era | None = None
    ) -> tuple[str, dict[str, Any]]:
        """The image this tree should be judged in, and why.

        Answered once per era. Every candidate in a release window reads the
        same manifests and reaches the same answer, so resolving per candidate
        re-walked the tree and re-read the same files for each one.
        """
        if era is not None:
            cached = self.by_era.get(era.key)
            if cached is not None:
                image, record = cached
                return image, {**record, "era": era.key, "reused": True}
        try:
            spec = resolve_runtime(
                tree, self.head.language, self.head, client=self.client, era=era
            )
        except Exception as exc:  # noqa: BLE001 — resolution must never end a run
            return self.head.image, {
                "status": "fallback",
                "reason": f"resolution_failed: {type(exc).__name__}: {exc}",
                "image": self.head.image,
            }

        if spec is None:
            # A tree that shadows the runner and cannot be pinned is not the same
            # as a tree with nothing to do, even though both end up on the HEAD
            # image. Saying so is the difference between a known limitation and
            # sixteen candidates failing for no recorded reason.
            record = {
                "status": "head_image",
                "reason": "cut_from_head",
                "image": self.head.image,
            }
            if era is not None:
                record["era"] = era.key
                self.by_era[era.key] = (self.head.image, record)
            return self.head.image, record

        tag = spec.tag(self.repository_name)
        if tag in self.built:
            return self.built[tag], {**self.records[tag], "reused": True}
        if len(self.built) >= max(1, min(self.expected_eras, _SAFETY_CEILING)):
            return self.head.image, {
                "status": "fallback",
                "reason": "era_ceiling_reached",
                "image": self.head.image,
                "spec": spec.to_dict(),
            }

        image, record = self._build(spec, tag)
        self.built[tag] = image
        self.records[tag] = record
        if era is not None:
            record = {**record, "era": era.key}
            self.by_era[era.key] = (image, record)
        self._persist()
        return image, record

    def _build(self, spec: RuntimeSpec, tag: str) -> tuple[str, dict[str, Any]]:
        from stress_stack.container import pin_image_by_digest

        base_image, pin_status = pin_image_by_digest(spec.base_image)
        dockerfile = render_dockerfile(spec, base_image)
        with tempfile.TemporaryDirectory(prefix="stress-stack-runtime-") as directory:
            context = Path(directory)
            (context / "Dockerfile").write_text(dockerfile, encoding="utf-8")
            result = run(
                ["docker", "build", "--tag", tag, "--file", "Dockerfile", "."],
                cwd=context,
                timeout=1800.0,
            )
        if not result.ok:
            tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-8:])
            return self.head.image, {
                "status": "fallback",
                "reason": "runtime_image_build_failed",
                "image": self.head.image,
                "spec": spec.to_dict(),
                "base_image": base_image,
                "build_log_tail": tail,
            }
        return tag, {
            "status": "per_candidate_image",
            # Keyed on the pin, not on the shadow: a tree can shadow the runner
            # and still have no pinnable version, and calling that image a
            # shadow fix would misreport what it does.
            "reason": (
                "tree_shadows_the_runner" if spec.install and "==" in spec.install[0]
                else "tree_wants_another_toolchain"
            ),
            "image": tag,
            "spec": spec.to_dict(),
            "base_image": base_image,
            "base_image_pin": pin_status,
        }

    def _persist(self) -> None:
        if self.stamp_path is None:
            return
        atomic_write_json(
            self.stamp_path,
            {
                "schema_version": "0.1.0",
                "head": {
                    "image": self.head.image,
                    "language": self.head.language,
                    "toolchain_version": self.head.toolchain_version,
                },
                "expected_eras": self.expected_eras,
                "images": {tag: self.records[tag] for tag in sorted(self.records)},
            },
        )

    def summary(self) -> dict[str, Any]:
        return {
            "head_image": self.head.image,
            "language": self.head.language,
            "head_toolchain": self.head.toolchain_version,
            "expected_eras": self.expected_eras,
            "built": sorted(self.built),
            "images": {tag: self.records[tag] for tag in sorted(self.records)},
        }
