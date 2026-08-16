"""Evidence that a *different* correct implementation would also pass.

Every other gate proves the reference solution satisfies the verifier. That is
necessary and not sufficient: the brief requires that "a different but correct
implementation must pass your verifier", and a test can satisfy the reference
while rejecting every other correct answer — by patching an internal helper, by
asserting on an exact ``repr``, by reaching for a private name.

Neither check here proves the property; nothing short of writing alternative
implementations would. Both detect the realistic ways it fails:

* **A static scan** of the verifier for the shapes that pin implementation
  rather than behaviour. Cheap, runs on every task, needs no container.
* **A semantics-preserving mutation** of the reference. Private symbols the
  change introduced are renamed and every reference updated, then the verifier
  runs again. Renaming something private cannot change observable behaviour, so
  a verifier that now fails was testing structure. Private names are chosen
  deliberately: they are internal by the language's own convention, so a correct
  alternative implementation is under no obligation to keep them.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Shapes that reject a correct alternative. Each is a way of asserting *how*
# rather than *what*.
_PATCH = re.compile(r"\b(?:mock\.)?patch(?:\.object)?\s*\(")
_EXACT_REPR = re.compile(r"\bassert\s+repr\s*\(|\bassert\s+str\s*\(|==\s*repr\s*\(")
_TYPE_IDENTITY = re.compile(r"\btype\s*\([^)]*\)\s*(?:is|==)\s")


@dataclass
class CouplingReport:
    """Where the verifier asserts implementation rather than behaviour."""

    private_references: list[str] = field(default_factory=list)
    patched_internals: list[str] = field(default_factory=list)
    exact_representation: list[str] = field(default_factory=list)
    type_identity: list[str] = field(default_factory=list)

    @property
    def findings(self) -> int:
        return (
            len(self.private_references)
            + len(self.patched_internals)
            + len(self.exact_representation)
            + len(self.type_identity)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings": self.findings,
            "private_references": self.private_references[:10],
            "patched_internals": self.patched_internals[:10],
            "exact_representation": self.exact_representation[:10],
            "type_identity": self.type_identity[:10],
        }


def scan_coupling(verifier_files: dict[str, str], private_names: set[str]) -> CouplingReport:
    """Look for assertions that only the reference implementation can satisfy.

    Reported, not gated. A test naming a private helper is a warning about how
    portable the verifier is; whether it actually rejects an alternative is what
    the mutation below measures.
    """
    report = CouplingReport()
    for path, source in sorted(verifier_files.items()):
        for number, line in enumerate(source.splitlines(), start=1):
            location = f"{path}:{number}"
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if _PATCH.search(line):
                report.patched_internals.append(location)
            if _EXACT_REPR.search(line):
                report.exact_representation.append(location)
            if _TYPE_IDENTITY.search(line):
                report.type_identity.append(location)
            for name in private_names:
                if re.search(rf"\b{re.escape(name)}\b", line):
                    report.private_references.append(f"{location} ({name})")
                    break
    return report


def private_symbols(source: str) -> set[str]:
    """Module-level private definitions — internal by the language's convention."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name.startswith("_") and not node.name.startswith("__"):
                names.add(node.name)
    return names


@dataclass(frozen=True, slots=True)
class Mutation:
    """A rename applied across a tree, and what it touched."""

    renames: dict[str, str]
    files_changed: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {"renames": self.renames, "files_changed": self.files_changed}


def rename_private(root: Path, targets: list[str], *, suffix: str = "_alt") -> Mutation:
    """Rename private module-level symbols throughout a materialised tree.

    A whole-word textual substitution rather than an AST rewrite, deliberately:
    the rewrite has to reach strings used with ``getattr`` and names mentioned in
    ``__all__``, and unparsing the whole tree would reformat files in ways that
    muddy what the verifier is reacting to. Word-boundary matching over a
    private, uniquely-spelled identifier is precise enough, and any failure to
    rename consistently surfaces immediately as an import error rather than
    silently passing.
    """
    renames: dict[str, str] = {}
    for relative in targets:
        path = root / relative
        if path.is_file():
            for name in private_symbols(path.read_text(encoding="utf-8", errors="replace")):
                renames[name] = f"{name}{suffix}"
    if not renames:
        return Mutation(renames={}, files_changed=[])

    changed: list[str] = []
    for path in sorted(root.rglob("*.py")):
        try:
            original = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        updated = original
        for old, new in renames.items():
            updated = re.sub(rf"\b{re.escape(old)}\b", new, updated)
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            changed.append(str(path.relative_to(root)))
    return Mutation(renames=renames, files_changed=changed)


def verdict(
    mutation: Mutation, still_passing: bool, coupling: CouplingReport, *, ran: bool
) -> dict[str, Any]:
    """What the two checks together say about verifier portability."""
    if not ran:
        status = "not_run"
    elif not mutation.renames:
        # Nothing private to rename is not a failure — it means the change
        # introduced no internal structure a verifier could have latched onto.
        status = "no_internals_to_rename"
    elif still_passing:
        status = "survived"
    else:
        status = "verifier_depends_on_internals"
    return {
        "status": status,
        "portable": status in {"survived", "no_internals_to_rename"},
        "mutation": mutation.to_dict(),
        "coupling": coupling.to_dict(),
    }
