"""Classify imports and turn the third-party ones into exact pins.

Imports fall into three tiers, and conflating any two of them produces a broken
manifest:

* ``internal``    — resolves to a file inside the repository (AlphaStack's rule:
  try ``pkg/mod.py`` then ``pkg/mod/__init__.py``, relative imports resolved
  against the importing file's directory).
* ``stdlib``      — shipped with Python. Never a dependency.
* ``third_party`` — an actual distribution to pin.

The import name is not the distribution name (``yaml`` ships in ``PyYAML``), so
the mapping is read from the provisioned environment with
``importlib.metadata.packages_distributions()`` rather than a hardcoded table.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from stress_stack.graph import RepositoryGraph, _absolute_module
from stress_stack.tooling import run

_MAX_ANCHORS = 5
_STDLIB = frozenset(sys.stdlib_module_names)

_PROBE = """
import json
from importlib.metadata import distributions, packages_distributions

mapping = {}
for top, dists in packages_distributions().items():
    mapping[top] = sorted(dists)
versions = {}
for dist in distributions():
    name = dist.metadata["Name"]
    if name:
        versions[name] = dist.version
print(json.dumps({"packages": mapping, "versions": versions}))
"""


@dataclass
class ImportFact:
    module: str
    kind: str
    distribution: str | None = None
    version: str | None = None
    test_only: bool = True
    always_guarded: bool = True
    guards: list[str] = field(default_factory=list)
    anchors: list[str] = field(default_factory=list)

    @property
    def bucket(self) -> str:
        """runtime, test, or optional — which lockfile this pin belongs in."""
        if self.always_guarded:
            return "optional"
        return "test" if self.test_only else "runtime"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["bucket"] = self.bucket if self.kind == "third_party" else None
        return value


@dataclass(frozen=True, slots=True)
class DependencyReport:
    facts: list[ImportFact]
    runtime: dict[str, str]
    test: dict[str, str]
    optional: dict[str, str]
    unpinned: list[str]
    declared: list[str]
    environment_available: bool

    def audit(self) -> dict[str, list[str]]:
        """Imports nobody declared, and declarations nobody imports."""
        imported = {
            fact.distribution
            for fact in self.facts
            if fact.kind == "third_party" and fact.distribution
        }
        declared = {_normalize(name) for name in self.declared}
        return {
            "imported_not_declared": sorted(
                name for name in imported if _normalize(name) not in declared
            ),
            "declared_not_imported": sorted(
                name
                for name in self.declared
                if _normalize(name) not in {_normalize(item) for item in imported}
            ),
            "unresolved_to_distribution": sorted(self.unpinned),
        }

    def to_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for fact in self.facts:
            counts[fact.kind] = counts.get(fact.kind, 0) + 1
        return {
            "schema_version": "0.1.0",
            "environment_available": self.environment_available,
            "counts": counts,
            "runtime_pins": dict(sorted(self.runtime.items())),
            "test_pins": dict(sorted(self.test.items())),
            "optional_pins": dict(sorted(self.optional.items())),
            "declared": sorted(self.declared),
            "audit": self.audit(),
            "imports": [
                fact.to_dict() for fact in sorted(self.facts, key=lambda item: item.module)
            ],
        }


def classify_imports(graph: RepositoryGraph) -> list[ImportFact]:
    """Bucket every top-level imported module into internal / stdlib / third_party."""
    repository_tops = {
        parsed.module.split(".")[0] for parsed in graph.files if parsed.module
    }
    facts: dict[str, ImportFact] = {}
    for parsed in graph.files:
        for reference in parsed.imports:
            if reference.level > 0:
                continue
            absolute = _absolute_module(
                parsed.module,
                reference.module,
                reference.level,
                is_package=parsed.path.endswith("__init__.py"),
            )
            top = (absolute or reference.bound_name).split(".")[0]
            if not top:
                continue
            if top in repository_tops:
                kind = "internal"
            elif top in _STDLIB:
                kind = "stdlib"
            else:
                kind = "third_party"
            fact = facts.get(top)
            if fact is None:
                fact = ImportFact(module=top, kind=kind)
                facts[top] = fact
            if not parsed.is_test:
                fact.test_only = False
            if reference.guard is None:
                fact.always_guarded = False
            elif reference.guard not in fact.guards:
                fact.guards.append(reference.guard)
            if len(fact.anchors) < _MAX_ANCHORS:
                fact.anchors.append(str(reference.anchor))
    return sorted(facts.values(), key=lambda item: item.module)


def build_report(
    graph: RepositoryGraph,
    python: Path | None,
    declared: list[str] | None = None,
) -> DependencyReport:
    facts = classify_imports(graph)
    environment = _probe_environment(python) if python is not None else None
    packages = (environment or {}).get("packages", {})
    versions = (environment or {}).get("versions", {})

    buckets: dict[str, dict[str, str]] = {"runtime": {}, "test": {}, "optional": {}}
    unpinned: list[str] = []
    for fact in facts:
        if fact.kind != "third_party":
            continue
        distribution = _distribution_for(fact.module, packages)
        if distribution is None:
            unpinned.append(fact.module)
            continue
        fact.distribution = distribution
        fact.version = versions.get(distribution)
        if fact.version is None:
            unpinned.append(fact.module)
            continue
        buckets[fact.bucket][distribution] = fact.version

    return DependencyReport(
        facts=facts,
        runtime=buckets["runtime"],
        test=buckets["test"],
        optional=buckets["optional"],
        unpinned=sorted(unpinned),
        declared=sorted(declared or []),
        environment_available=environment is not None,
    )


def render_lockfile(pins: dict[str, str], *, header: str) -> str:
    lines = [f"# {line}" for line in header.splitlines()]
    lines.append("")
    lines.extend(f"{name}=={version}" for name, version in sorted(pins.items()))
    return "\n".join(lines) + "\n"


def _probe_environment(python: Path) -> dict[str, Any] | None:
    result = run([str(python), "-c", _PROBE], timeout=120.0)
    if not result.ok:
        return None
    try:
        payload = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _distribution_for(module: str, packages: dict[str, Any]) -> str | None:
    candidates = packages.get(module)
    if isinstance(candidates, list) and candidates:
        return str(candidates[0])
    return None


def _normalize(name: str) -> str:
    """PEP 503 normalisation, so ``PyYAML`` and ``pyyaml`` compare equal."""
    return "".join("-" if character in "-_." else character for character in name.lower())
