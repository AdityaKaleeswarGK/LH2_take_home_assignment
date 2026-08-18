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
they get one answer, and the answer is read out of the tree rather than
configured. Each resolver in ``_RESOLVERS`` explores a candidate's own manifests
and CI files and reports what it found; adding an ecosystem is adding a resolver,
in the same shape as ``test_runners._PLANS`` and ``linters._DISPATCH``.

Two things this deliberately does not do:

* **It does not ask a model.** Which command runs the suite decides which tasks
  pass, and the whole point of this pipeline is that no model decides that. Every
  field below is read from a file in the tree, and ``evidence`` records which
  file it came from.
* **It does not guess compatibility.** When a tree owns a package the runner also
  needs, that package is pinned to the tree's own version and the runner is left
  unpinned, so the ecosystem's own resolver picks a runner that accepts it. The
  knowledge lives in the index, where it is correct, instead of in this file,
  where it would rot.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from stress_stack.atomic import atomic_write_json
from stress_stack.tooling import run

# Packages the Python test runner itself imports. A repository that *is* one of
# these cannot be verified against a runner built for a different version of it.
RUNNER_DEPENDENCIES = frozenset(
    {"pluggy", "iniconfig", "packaging", "exceptiongroup", "tomli", "pytest", "_pytest"}
)

# Top-level import names whose distribution is not the same string.
_DISTRIBUTION_OF = {"_pytest": "pytest"}

_SAFE_REQUIREMENT = re.compile(r"^[A-Za-z0-9._\-]+(\[[A-Za-z0-9,._\-]+\])?[<>=!~ 0-9.,*]*$")
_SAFE_PACKAGE = re.compile(r"^[a-z0-9][a-z0-9+.\-]*$")
_SETUP_NAME_RE = re.compile(r"""\bname\s*=\s*['"]([^'"]+)['"]""")
_SETUP_VERSION_RE = re.compile(r"""\bversion\s*=\s*['"]([0-9][^'"]*)['"]""")


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


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        import tomllib

        return tomllib.loads(_read(path))
    except (ValueError, ImportError):
        return {}


def ci_facts(tree: Path) -> Any:
    """The tree's own CI configuration, or empty facts when it has none."""
    from stress_stack.ci_parser import CIParsedFacts, parse_ci_facts

    try:
        return parse_ci_facts(tree)
    except Exception:  # noqa: BLE001 — a malformed workflow must not end a run
        return CIParsedFacts()


def system_packages_from_ci(tree: Path) -> tuple[str, ...]:
    """apt packages the project's own CI installs before running its suite.

    A historical suite that needs libxml2 fails in an image that does not have
    it, and the project already wrote down that it needs it. Filtered to plain
    package names — nothing from a CI file reaches a shell unchecked.
    """
    found = {
        package
        for package in getattr(ci_facts(tree), "system_packages", [])
        if isinstance(package, str) and _SAFE_PACKAGE.match(package)
    }
    return tuple(sorted(found))


def shadowed_distributions(tree: Path) -> tuple[str, ...]:
    """Runner dependencies this tree provides its own copy of.

    Derived from the same ``source_roots`` the run itself will use, so the answer
    is about what will actually be importable rather than what the layout looks
    like. Python-only by nature: no other ecosystem here resolves its test runner
    through a path the task tree is mounted onto.
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


def project_metadata(tree: Path) -> tuple[str | None, str | None, str | None]:
    """The tree's own distribution name, version and source file.

    Read from the *candidate's* manifest rather than HEAD's, because that is the
    whole point: a 2018 commit declares the version a 2018-compatible runner has
    to agree with.
    """
    project = _toml(tree / "pyproject.toml").get("project")
    if isinstance(project, dict) and isinstance(project.get("name"), str):
        version = project.get("version")
        return project["name"], version if isinstance(version, str) else None, "pyproject.toml"

    config = tree / "setup.cfg"
    if config.is_file():
        text = _read(config)
        name = re.search(r"^\s*name\s*=\s*(\S+)", text, re.MULTILINE)
        version = re.search(r"^\s*version\s*=\s*([0-9]\S*)", text, re.MULTILINE)
        if name:
            return name.group(1).strip(), version.group(1).strip() if version else None, "setup.cfg"

    setup = tree / "setup.py"
    if setup.is_file():
        text = _read(setup)
        name = _SETUP_NAME_RE.search(text)
        version = _SETUP_VERSION_RE.search(text)
        if name:
            return name.group(1), version.group(1) if version else None, "setup.py"
    return None, None, None


def declared_requires_python(tree: Path) -> tuple[str | None, str | None]:
    project = _toml(tree / "pyproject.toml").get("project")
    if isinstance(project, dict) and isinstance(project.get("requires-python"), str):
        return project["requires-python"], "pyproject.toml"
    config = tree / "setup.cfg"
    if config.is_file():
        match = re.search(r"^\s*python_requires\s*=\s*(.+)$", _read(config), re.MULTILINE)
        if match:
            return match.group(1).strip(), "setup.cfg"
    setup = tree / "setup.py"
    if setup.is_file():
        match = re.search(r"""python_requires\s*=\s*['"]([^'"]+)['"]""", _read(setup))
        if match:
            return match.group(1), "setup.py"
    return None, None


def declared_dependencies(tree: Path) -> tuple[str, ...]:
    """The tree's own runtime dependencies, as declared, unpinned.

    Best effort and deliberately unresolved: these exist so that importing the
    package under test does not fail for want of a third-party module. Anything
    that does not look like a plain requirement is dropped rather than passed to
    a shell.
    """
    declared = (_toml(tree / "pyproject.toml").get("project") or {}).get("dependencies")
    if not isinstance(declared, list):
        return ()
    return tuple(
        sorted(
            item for item in declared if isinstance(item, str) and _SAFE_REQUIREMENT.match(item)
        )
    )


# --------------------------------------------------------------------------
# Per-ecosystem resolvers
# --------------------------------------------------------------------------


def _resolve_python(tree: Path, head: "HeadRuntime") -> RuntimeSpec | None:
    from stress_stack.container import _CANDIDATE_PYTHONS, _satisfies, select_python_version

    evidence: dict[str, Any] = {}
    declared, declared_from = declared_requires_python(tree)
    version = select_python_version(declared) if declared else head.toolchain_version
    if declared:
        evidence["requires_python"] = {"value": declared, "from": declared_from}

    # CI is an independent second opinion, and a more concrete one: a matrix
    # names versions the project actually ran on, where requires-python only
    # names a floor.
    matrix = [v for v in getattr(ci_facts(tree), "matrix_versions", []) if v in _CANDIDATE_PYTHONS]
    agreed = [
        candidate
        for candidate in _CANDIDATE_PYTHONS
        if candidate in matrix and (not declared or _satisfies(candidate, declared))
    ]
    if agreed:
        version = agreed[0]
        evidence["ci_matrix"] = {"value": sorted(matrix), "chose": version}

    shadowed = shadowed_distributions(tree)
    pins: list[str] = []
    if shadowed:
        name, own_version, source = project_metadata(tree)
        normalized = (name or "").replace("_", "-").lower()
        if normalized and own_version and normalized in {s.replace("_", "-") for s in shadowed}:
            pins.append(f"{name}=={own_version}")
            evidence["self_hosted"] = {
                "distribution": name,
                "version": own_version,
                "from": source,
            }

    if not pins and version == head.toolchain_version:
        # Nothing about this tree differs from the image that already exists —
        # and that image carries the real pinned lock, which a synthesized one
        # cannot.
        return None

    requirements = [*pins, "pytest", *declared_dependencies(tree)]
    return RuntimeSpec(
        language="python",
        base_image=f"python:{version}-slim",
        install=(f"pip install {' '.join(_quote(item) for item in requirements)}",),
        system_packages=system_packages_from_ci(tree),
        shadowed=shadowed,
        test_command=("python", "-m", "pytest"),
        evidence=evidence,
    )


def _resolve_go(tree: Path, head: "HeadRuntime") -> RuntimeSpec | None:
    """Go's toolchain version is declared in go.mod and nowhere else that matters.

    No dependency step: Go resolves modules from the network, which validation
    does not have, so a candidate whose module set differs from HEAD's still has
    to use HEAD's image. The toolchain is what this can honestly move.
    """
    match = re.search(r"^go\s+(\d+\.\d+)", _read(tree / "go.mod"), re.MULTILINE)
    if not match or match.group(1) == head.toolchain_version:
        return None
    return RuntimeSpec(
        language="go",
        base_image=f"golang:{match.group(1)}-bookworm",
        system_packages=system_packages_from_ci(tree),
        test_command=("go", "test", "./..."),
        evidence={"go_directive": {"value": match.group(1), "from": "go.mod"}},
    )


def _resolve_rust(tree: Path, head: "HeadRuntime") -> RuntimeSpec | None:
    toolchain = _toml(tree / "rust-toolchain.toml").get("toolchain")
    version = toolchain.get("channel") if isinstance(toolchain, dict) else None
    source = "rust-toolchain.toml"
    if not isinstance(version, str):
        package = _toml(tree / "Cargo.toml").get("package")
        version = package.get("rust-version") if isinstance(package, dict) else None
        source = "Cargo.toml"
    if not isinstance(version, str) or version == head.toolchain_version:
        return None
    if not re.fullmatch(r"[0-9][0-9.]*|stable|beta|nightly", version):
        return None
    return RuntimeSpec(
        language="rust",
        base_image=f"rust:{version}-bookworm",
        system_packages=system_packages_from_ci(tree),
        test_command=("cargo", "test"),
        evidence={"toolchain": {"value": version, "from": source}},
    )


def _resolve_node(tree: Path, head: "HeadRuntime") -> RuntimeSpec | None:
    version = _read(tree / ".nvmrc").strip().lstrip("v")
    source = ".nvmrc"
    if not version:
        try:
            engines = json.loads(_read(tree / "package.json") or "{}").get("engines") or {}
        except ValueError:
            engines = {}
        declared = str(engines.get("node") or "")
        match = re.search(r"(\d+)", declared)
        version = match.group(1) if match else ""
        source = "package.json"
    if not version or not re.fullmatch(r"\d+(\.\d+)*", version):
        return None
    if version.split(".")[0] == head.toolchain_version.split(".")[0]:
        return None
    return RuntimeSpec(
        language="typescript",
        base_image=f"node:{version.split('.')[0]}-slim",
        system_packages=system_packages_from_ci(tree),
        test_command=("npm", "test"),
        evidence={"node_version": {"value": version, "from": source}},
    )


# Adding an ecosystem is adding a row, in the same shape as
# `test_runners._PLANS` and `linters._DISPATCH`. A resolver returns None to mean
# "the image the container stage already proved is right for this tree", which
# is the correct answer far more often than not.
_RESOLVERS: dict[str, Callable[[Path, "HeadRuntime"], RuntimeSpec | None]] = {
    "python": _resolve_python,
    "go": _resolve_go,
    "rust": _resolve_rust,
    "typescript": _resolve_node,
    "javascript": _resolve_node,
}


def supported_languages() -> frozenset[str]:
    return frozenset(_RESOLVERS)


def resolve_runtime(tree: Path, language: str, head: "HeadRuntime") -> RuntimeSpec | None:
    resolver = _RESOLVERS.get(language)
    if resolver is None:
        return None
    return resolver(tree, head)


def _quote(value: str) -> str:
    return '"' + value.replace('"', "") + '"'


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

    The budget is less a performance guard than a blast radius: a repository
    whose history spans four toolchains is worth four images, and one whose every
    commit resolves differently is telling us the resolution is wrong. Exhausting
    it falls back to the HEAD image and records that it did, so a degraded
    verdict is never silent.
    """

    repository_name: str
    head: HeadRuntime
    stamp_path: Path | None = None
    budget: int = 4
    built: dict[str, str] = field(default_factory=dict)
    records: dict[str, dict[str, Any]] = field(default_factory=dict)

    def runtime_for(self, tree: Path) -> tuple[str, dict[str, Any]]:
        """The image this tree should be judged in, and why."""
        try:
            spec = resolve_runtime(tree, self.head.language, self.head)
        except Exception as exc:  # noqa: BLE001 — resolution must never end a run
            return self.head.image, {
                "status": "fallback",
                "reason": f"resolution_failed: {type(exc).__name__}: {exc}",
                "image": self.head.image,
            }

        if spec is None:
            return self.head.image, {
                "status": "head_image",
                "reason": "tree_matches_head_runtime",
                "image": self.head.image,
            }

        tag = spec.tag(self.repository_name)
        if tag in self.built:
            return self.built[tag], {**self.records[tag], "reused": True}
        if len(self.built) >= self.budget:
            return self.head.image, {
                "status": "fallback",
                "reason": "image_budget_exhausted",
                "image": self.head.image,
                "spec": spec.to_dict(),
            }

        image, record = self._build(spec, tag)
        self.built[tag] = image
        self.records[tag] = record
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
            "reason": (
                "tree_shadows_the_runner" if spec.shadowed else "tree_wants_another_toolchain"
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
                "budget": self.budget,
                "images": {tag: self.records[tag] for tag in sorted(self.records)},
            },
        )

    def summary(self) -> dict[str, Any]:
        return {
            "head_image": self.head.image,
            "language": self.head.language,
            "head_toolchain": self.head.toolchain_version,
            "budget": self.budget,
            "built": sorted(self.built),
            "images": {tag: self.records[tag] for tag in sorted(self.records)},
        }
