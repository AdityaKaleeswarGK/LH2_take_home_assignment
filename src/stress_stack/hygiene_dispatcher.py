"""Multi-language formatting, reported as what the formatter actually changed.

Two things this module refuses to do, because both were producing numbers that
looked like evidence and were not:

* count every source file in the tree as "reformatted" — the reformatted count
  comes from ``git diff``, so it is the number of files the formatter touched;
* claim a zero-regression result without having measured regressions. Only the
  Python path compares a before/after test snapshot today, so only the Python
  path reports one. Everything else sets ``regressions_verified=False`` and says
  why, which the container stage can later answer properly.

Formatting still runs on the host for non-Python ecosystems. That is a known
gap, recorded in ``reason`` rather than papered over.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stress_stack.project_detector import ProjectProfile, detect_project_profile
from stress_stack.tooling import run

COMPLETE = "complete"
# Formatting succeeded but no before/after suite snapshot was taken, so this run
# cannot claim it introduced no regressions. Deliberately a different status from
# `complete`: the pipeline gate treats the two differently, and collapsing them
# would let an unverified ecosystem present itself as a verified one. The name
# matches the vocabulary `hygiene._status` already uses for the same situation.
COMPLETE_UNVERIFIED = "complete_unverified"
UNSUPPORTED = "unsupported"
FAILED = "failed"
# Hygiene changed behaviour. `reverted` means the tree was restored and is safe
# to continue from; `regressed` means it was not, and the tree is now suspect.
REVERTED = "reverted"
REGRESSED = "regressed"

# Which suffixes count as "reformatted" per ecosystem, so the count after
# linting measures the same file set the formatter measured.
_SUFFIXES: dict[str, tuple[str, ...]] = {
    "rust": (".rs",),
    "go": (".go",),
    "typescript": (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"),
    "javascript": (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"),
    "c": (".c", ".cc", ".cpp", ".h", ".hpp"),
    "cpp": (".c", ".cc", ".cpp", ".h", ".hpp"),
}


@dataclass(frozen=True, slots=True)
class HygieneReport:
    status: str
    language: str
    tool: str
    files_reformatted: int
    violations_fixed: int
    # None where no snapshot was taken — distinct from a measured zero.
    before_tests_passing: int | None
    after_tests_passing: int | None
    regressions: int | None
    regressions_verified: bool
    reason: str = ""
    regressed_tests: list[str] = field(default_factory=list)
    lint: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "language": self.language,
            "tool": self.tool,
            "files_reformatted": self.files_reformatted,
            "violations_fixed": self.violations_fixed,
            "before_tests_passing": self.before_tests_passing,
            "after_tests_passing": self.after_tests_passing,
            "regressions": self.regressions,
            "regressions_verified": self.regressions_verified,
            "reason": self.reason,
            "regressed_tests": self.regressed_tests,
            "lint": self.lint,
        }


def _unsupported(language: str, reason: str, tool: str = "none") -> HygieneReport:
    return HygieneReport(
        status=UNSUPPORTED,
        language=language,
        tool=tool,
        files_reformatted=0,
        violations_fixed=0,
        before_tests_passing=None,
        after_tests_passing=None,
        regressions=None,
        regressions_verified=False,
        reason=reason,
    )


def _hygiene_python(root: Path) -> HygieneReport:
    """Wrap the verified Python path, reading the fields it actually exposes.

    ``hygiene.HygieneResult`` carries a ``lint: LintResult`` and two
    ``PytestRunResult`` snapshots; the counts live on those, not on the top
    level. ``regressions`` is a property returning the regressed node ids.
    """
    from stress_stack.hygiene import run_hygiene

    legacy = run_hygiene(str(root))
    regressed = list(legacy.regressions)
    return HygieneReport(
        status=legacy.status,
        language="python",
        tool="ruff",
        files_reformatted=legacy.lint.files_reformatted,
        violations_fixed=legacy.lint.fixed,
        before_tests_passing=len(legacy.tests_before.passing),
        after_tests_passing=len(legacy.tests_after.passing),
        regressions=len(regressed),
        regressions_verified=True,
        regressed_tests=regressed,
    )


def _owned_sources(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    """Source files the repository owns, excluding our own working directory.

    `.stress_stack/` holds the staged task trees, and an excision task's `input/`
    contains a deliberately unimplemented body. Formatting the whole tree walked
    into those and failed on our own artifact — `gofmt` reported
    `expected declaration, found panic` and took the hygiene stage down with it.
    Vendored and hidden directories are excluded for the same reason the graph
    excludes them: they are not this repository's code.
    """
    skip = {".stress_stack", ".git", "node_modules", "vendor", "target", "build", "dist"}
    return [
        path
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and path.suffix in suffixes
        and not any(part in skip for part in path.relative_to(root).parts)
        and not any(part.startswith(".") for part in path.relative_to(root).parts[:-1])
    ]


def _changed_file_count(root: Path, suffixes: tuple[str, ...]) -> int:
    """How many tracked files the formatter actually modified."""
    result = run(["git", "diff", "--name-only"], cwd=root, timeout=120.0)
    if not result.ok:
        return 0
    return sum(
        1
        for line in result.stdout.splitlines()
        if line.strip().endswith(suffixes)
    )


def dispatch_hygiene(
    repo_root: Path | str,
    profile: ProjectProfile | None = None,
    *,
    client: Any | None = None,
) -> HygieneReport:
    """Format, lint, verify in a container, and repair what is left.

    ``client`` enables the repair agent for violations the linter's own autofix
    could not clear. Without one the stage still formats, lints and verifies —
    the agent is an addition, never a prerequisite.
    """
    root = Path(repo_root)
    prof = profile or detect_project_profile(root)
    lang = prof.primary_language

    if lang == "python":
        return _hygiene_python(root)

    # Snapshot the suite in a container before touching anything, so a
    # regression can be detected and undone rather than reported after the fact.
    from stress_stack.hygiene_verify import (
        build_probe_image,
        compare,
        revert_working_tree,
        snapshot_suite,
    )

    probe_image, probe_reason = build_probe_image(root, prof)
    before_snapshot = snapshot_suite(probe_image) if probe_image else None

    if lang == "rust":
        if not shutil.which("cargo"):
            return _unsupported("rust", "cargo_not_installed")
        result = run(["cargo", "fmt", "--all"], cwd=root, timeout=600.0)
        if not result.ok:
            return HygieneReport(
                status=FAILED,
                language="rust",
                tool="cargo fmt",
                files_reformatted=0,
                violations_fixed=0,
                before_tests_passing=None,
                after_tests_passing=None,
                regressions=None,
                regressions_verified=False,
                reason=result.failure_detail(),
            )
        changed = _changed_file_count(root, (".rs",))
        tool = "cargo fmt"

    elif lang in {"typescript", "javascript"}:
        if not shutil.which("npx"):
            return _unsupported(lang, "npx_not_installed")
        # `--no-install` keeps hygiene offline and deterministic: `--yes` would
        # fetch and execute an unpinned package from the registry mid-run.
        result = run(
            [
                "npx",
                "--no-install",
                "prettier",
                "--write",
                "--ignore-path",
                ".gitignore",
                "--ignore-pattern",
                ".stress_stack/**",
                ".",
            ],
            cwd=root,
            timeout=900.0,
        )
        if not result.ok:
            return _unsupported(
                lang,
                "prettier_not_available_locally: add it to devDependencies",
                tool="prettier",
            )
        changed = _changed_file_count(root, (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"))
        tool = "prettier"

    elif lang == "go":
        if not shutil.which("gofmt"):
            return _unsupported("go", "gofmt_not_installed")
        targets = _owned_sources(root, (".go",))
        result = (
            run(["gofmt", "-w", *[str(p) for p in targets]], cwd=root, timeout=600.0)
            if targets
            else run(["gofmt", "--help"], cwd=root, timeout=60.0)
        )
        if targets and not result.ok:
            return HygieneReport(
                status=FAILED,
                language="go",
                tool="gofmt",
                files_reformatted=0,
                violations_fixed=0,
                before_tests_passing=None,
                after_tests_passing=None,
                regressions=None,
                regressions_verified=False,
                reason=result.failure_detail(),
            )
        changed = _changed_file_count(root, (".go",))
        tool = "gofmt"

    elif lang in {"c", "cpp"}:
        if not shutil.which("clang-format"):
            return _unsupported(lang, "clang_format_not_installed")
        targets = _owned_sources(root, (".c", ".cc", ".cpp", ".h", ".hpp"))
        if targets:
            run(
                ["clang-format", "-i", *[str(p) for p in targets]],
                cwd=root,
                timeout=900.0,
            )
        changed = _changed_file_count(root, (".c", ".cc", ".cpp", ".h", ".hpp"))
        tool = "clang-format"

    else:
        return _unsupported(lang, f"no_formatter_for_{lang}")

    # Linting comes after formatting so the two cannot fight: a formatter that
    # rewrites what a linter just fixed would leave the tree neither clean nor
    # stable across runs.
    from stress_stack.linters import lint

    lint_outcome = lint(root, lang)
    if lint_outcome.status == "linted":
        changed = _changed_file_count(root, _SUFFIXES.get(lang, ()))

    if before_snapshot is None or not before_snapshot.usable:
        return HygieneReport(
            status=COMPLETE_UNVERIFIED,
            language=lang,
            tool=f"{tool} + {lint_outcome.tool}",
            files_reformatted=changed,
            violations_fixed=lint_outcome.fixed,
            before_tests_passing=None,
            after_tests_passing=None,
            regressions=None,
            regressions_verified=False,
            reason=probe_reason or "no_before_snapshot",
            lint=lint_outcome.to_dict(),
        )

    # The image was built from the pre-hygiene tree, so it has to be rebuilt to
    # see the formatted sources; comparing against the stale image would compare
    # the tree to itself.
    rebuilt_image, rebuild_reason = build_probe_image(root, prof)
    after_snapshot = snapshot_suite(rebuilt_image) if rebuilt_image else None
    if after_snapshot is None or not after_snapshot.usable:
        return HygieneReport(
            status=COMPLETE_UNVERIFIED,
            language=lang,
            tool=f"{tool} + {lint_outcome.tool}",
            files_reformatted=changed,
            violations_fixed=lint_outcome.fixed,
            before_tests_passing=None,
            after_tests_passing=None,
            regressions=None,
            regressions_verified=False,
            reason=rebuild_reason or "no_after_snapshot",
            lint=lint_outcome.to_dict(),
        )

    regressed, why = compare(before_snapshot, after_snapshot)

    # Only attempt repair once the tree is known good: repairing on top of an
    # already-regressed tree would make the revert ambiguous.
    repair: dict[str, Any] = {}
    if not regressed and client is not None and lint_outcome.violations_after:
        from stress_stack.lint_repair import repair_lint_violations

        outcome = repair_lint_violations(
            root,
            lang,
            client=client,
            probe_image=rebuilt_image,
            baseline_output=after_snapshot.normalized,
        )
        repair = outcome.to_dict()
        lint_outcome.violations_after = outcome.violations_end
        lint_outcome.fixed += outcome.to_dict()["violations_cleared"]

    if regressed:
        reverted = revert_working_tree(root)
        return HygieneReport(
            status=REGRESSED if not reverted else REVERTED,
            language=lang,
            tool=f"{tool} + {lint_outcome.tool}",
            files_reformatted=0 if reverted else changed,
            violations_fixed=0 if reverted else lint_outcome.fixed,
            before_tests_passing=None,
            after_tests_passing=None,
            regressions=1,
            regressions_verified=True,
            reason=f"{why}; formatting {'reverted' if reverted else 'could not be reverted'}",
            lint=lint_outcome.to_dict(),
        )

    return HygieneReport(
        status=COMPLETE,
        language=lang,
        tool=f"{tool} + {lint_outcome.tool}",
        files_reformatted=changed,
        violations_fixed=lint_outcome.fixed,
        before_tests_passing=None,
        after_tests_passing=None,
        regressions=0,
        regressions_verified=True,
        reason=lint_outcome.reason,
        lint={**lint_outcome.to_dict(), "repair": repair},
    )
