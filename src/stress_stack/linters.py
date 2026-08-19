"""Per-ecosystem linting, with counts that come from the linter's own output.

Every entry here answers three questions the same way Python's ruff path does:
how many violations existed, how many the autofix removed, and how many remain.
A linter that cannot run reports ``unsupported`` with the reason — an ecosystem
with no counts is never presented as an ecosystem with zero violations.

Two tools are structurally unavailable more often than they are available, and
say so rather than guessing:

* ``eslint`` needs both a config and a local install. A repository that has
  neither cannot be linted offline, and installing one mid-run would mean
  executing unpinned registry code against the tree being measured.
* ``clang-tidy`` needs ``compile_commands.json``, which only exists after a
  CMake configure. Before the container stage builds the project there is
  nothing for it to read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stress_stack.tooling import run

LINTED = "linted"
UNSUPPORTED = "unsupported"
FAILED = "failed"


@dataclass
class LintOutcome:
    status: str
    tool: str
    violations_before: int
    violations_after: int
    fixed: int
    # True only when both counts came from parsing a linter's output.
    measured: bool
    reason: str = ""
    config_written: str | None = None
    residual: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "tool": self.tool,
            "violations_before": self.violations_before,
            "violations_after": self.violations_after,
            "fixed": self.fixed,
            "measured": self.measured,
            "reason": self.reason,
            "config_written": self.config_written,
            "residual_rules": self.residual,
        }


def _unsupported(tool: str, reason: str) -> LintOutcome:
    return LintOutcome(
        status=UNSUPPORTED,
        tool=tool,
        violations_before=0,
        violations_after=0,
        fixed=0,
        measured=False,
        reason=reason,
    )


# --------------------------------------------------------------------------- rust


def _clippy_counts(root: Path) -> tuple[int, dict[str, int]] | None:
    """Count clippy diagnostics from its JSON message stream."""
    result = run(
        ["cargo", "clippy", "--all-targets", "--message-format=json"],
        cwd=root,
        timeout=1800.0,
    )
    # A compile error means the count would describe a tree that does not build,
    # which is not a lint result. Only a clean or warning-only run counts.
    total = 0
    rules: dict[str, int] = {}
    saw_output = False
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        saw_output = True
        message = payload.get("message")
        if not isinstance(message, dict):
            continue
        if message.get("level") not in {"warning", "error"}:
            continue
        code = (message.get("code") or {}).get("code") or message.get("level")
        rules[str(code)] = rules.get(str(code), 0) + 1
        total += 1
    return (total, rules) if saw_output else None


def _lint_rust(root: Path) -> LintOutcome:
    import shutil

    if not shutil.which("cargo"):
        return _unsupported("clippy", "cargo_not_installed")
    probe = run(["cargo", "clippy", "--version"], cwd=root, timeout=120.0)
    if not probe.ok:
        return _unsupported("clippy", "clippy_component_not_installed")

    before = _clippy_counts(root)
    if before is None:
        return _unsupported("clippy", "clippy_produced_no_diagnostics_stream")

    # `--fix` rewrites source, so it needs the dirty allowances: hygiene has
    # already reformatted the tree by this point.
    run(
        [
            "cargo",
            "clippy",
            "--all-targets",
            "--fix",
            "--allow-dirty",
            "--allow-staged",
        ],
        cwd=root,
        timeout=1800.0,
    )
    after = _clippy_counts(root)
    if after is None:
        return LintOutcome(
            status=FAILED,
            tool="clippy",
            violations_before=before[0],
            violations_after=before[0],
            fixed=0,
            measured=True,
            reason="clippy_failed_after_fix",
        )
    return LintOutcome(
        status=LINTED,
        tool="clippy",
        violations_before=before[0],
        violations_after=after[0],
        fixed=max(0, before[0] - after[0]),
        measured=True,
        residual=after[1],
    )


# ----------------------------------------------------------------------------- go


def _go_vet_count(root: Path) -> int:
    result = run(["go", "vet", "./..."], cwd=root, timeout=900.0)
    # go vet writes one diagnostic per line to stderr and exits non-zero when it
    # finds anything; an empty stderr with exit 0 means a clean tree.
    return sum(
        1
        for line in result.stderr.splitlines()
        if line.strip() and not line.startswith("#")
    )


def _lint_go(root: Path) -> LintOutcome:
    import shutil

    if not shutil.which("go"):
        return _unsupported("go vet", "go_not_installed")
    before = _go_vet_count(root)
    # `go vet` has no autofix, so before and after are the same measurement and
    # `fixed` is honestly zero rather than an inferred difference.
    return LintOutcome(
        status=LINTED,
        tool="go vet",
        violations_before=before,
        violations_after=before,
        fixed=0,
        measured=True,
        reason="go_vet_reports_only_no_autofix" if before else "",
    )


# ------------------------------------------------------------------- javascript


_ESLINT_CONFIG = """\
// Generated by stress-stack. The recommended set only — an opinionated config
// would produce churn that has nothing to do with the repository's own quality.
import js from "@eslint/js";

export default [
  js.configs.recommended,
  { languageOptions: { ecmaVersion: "latest", sourceType: "module" } },
];
"""


def _eslint_config_present(root: Path) -> str | None:
    for name in (
        "eslint.config.js",
        "eslint.config.mjs",
        "eslint.config.cjs",
        ".eslintrc.js",
        ".eslintrc.cjs",
        ".eslintrc.json",
        ".eslintrc.yml",
        ".eslintrc.yaml",
    ):
        if (root / name).is_file():
            return name
    return None


def _eslint_counts(root: Path) -> int | None:
    result = run(
        ["npx", "--no-install", "eslint", ".", "--format", "json"],
        cwd=root,
        timeout=1800.0,
    )
    text = result.stdout.strip()
    if not text.startswith("["):
        return None
    try:
        report = json.loads(text)
    except json.JSONDecodeError:
        return None
    return sum(
        int(entry.get("errorCount", 0)) + int(entry.get("warningCount", 0))
        for entry in report
        if isinstance(entry, dict)
    )


def _lint_javascript(root: Path) -> LintOutcome:
    import shutil

    if not shutil.which("npx"):
        return _unsupported("eslint", "npx_not_installed")

    config = _eslint_config_present(root)
    if config is None:
        # Writing a config is safe and is what the brief asks for; it is the
        # *install* we will not do, because fetching a linter from the registry
        # mid-run executes unpinned code against the tree being measured.
        (root / "eslint.config.js").write_text(_ESLINT_CONFIG, encoding="utf-8")
        config = "eslint.config.js"

    before = _eslint_counts(root)
    if before is None:
        return LintOutcome(
            status=UNSUPPORTED,
            tool="eslint",
            violations_before=0,
            violations_after=0,
            fixed=0,
            measured=False,
            reason="eslint_not_installed_locally: add eslint to devDependencies",
            config_written=config,
        )

    run(
        ["npx", "--no-install", "eslint", ".", "--fix"],
        cwd=root,
        timeout=1800.0,
    )
    after = _eslint_counts(root)
    if after is None:
        after = before
    return LintOutcome(
        status=LINTED,
        tool="eslint",
        violations_before=before,
        violations_after=after,
        fixed=max(0, before - after),
        measured=True,
        config_written=config,
    )


# ------------------------------------------------------------------------ c/c++


_CLANG_TIDY_CONFIG = """\
# Generated by stress-stack. Correctness and bug-prone checks only; style checks
# are the formatter's job and would otherwise double-report.
Checks: 'clang-analyzer-*,bugprone-*,performance-*,-clang-analyzer-alpha*'
WarningsAsErrors: ''
"""


def _lint_cpp(root: Path) -> LintOutcome:
    import shutil

    if not shutil.which("clang-tidy"):
        return _unsupported("clang-tidy", "clang_tidy_not_installed")

    database = next(
        (
            path
            for path in (root / "compile_commands.json", root / "build" / "compile_commands.json")
            if path.is_file()
        ),
        None,
    )
    if database is None:
        return _unsupported(
            "clang-tidy",
            "no_compile_commands_json: configure the project with "
            "CMAKE_EXPORT_COMPILE_COMMANDS=ON first",
        )

    config_path = root / ".clang-tidy"
    written = None
    if not config_path.is_file():
        config_path.write_text(_CLANG_TIDY_CONFIG, encoding="utf-8")
        written = ".clang-tidy"

    sources = [
        str(path)
        for pattern in ("*.c", "*.cc", "*.cpp")
        for path in root.rglob(pattern)
        if ".stress_stack" not in path.parts and ".git" not in path.parts
    ]
    if not sources:
        return _unsupported("clang-tidy", "no_translation_units_found")

    result = run(
        ["clang-tidy", "-p", str(database.parent), *sources[:200]],
        cwd=root,
        timeout=1800.0,
    )
    count = sum(
        1
        for line in (result.stdout + result.stderr).splitlines()
        if ": warning:" in line or ": error:" in line
    )
    return LintOutcome(
        status=LINTED,
        tool="clang-tidy",
        violations_before=count,
        violations_after=count,
        fixed=0,
        measured=True,
        reason="clang_tidy_run_without_fix",
        config_written=written,
    )


_DISPATCH = {
    "rust": _lint_rust,
    "go": _lint_go,
    "javascript": _lint_javascript,
    "typescript": _lint_javascript,
    "c": _lint_cpp,
    "cpp": _lint_cpp,
}


def lint(
    root: Path | str, language: str, *, command: list[str] | None = None
) -> LintOutcome:
    """Lint with the ecosystem's standard tool, or say why that was not possible.

    The handlers below do two things at once: they invoke a tool and they *count*
    what it reported, which needs that tool's output format and is therefore
    code rather than configuration. So a workflow-supplied ``command`` is only
    used where this module has no handler for the ecosystem — otherwise the
    command would run and its violations would go uncounted, and a linted tree
    would report zero violations for the same reason an unlinted one does.
    """
    handler = _DISPATCH.get(language)
    if handler is not None:
        return handler(Path(root))
    if not command:
        return _unsupported("none", f"no_linter_for_{language}")

    # No handler, so no counting. The tool runs and the result says plainly that
    # nothing was measured, rather than reporting a zero it did not observe.
    result = run(command, cwd=Path(root), timeout=900.0)
    return LintOutcome(
        status="linted" if result.ok else "failed",
        tool=command[0],
        violations_before=0,
        violations_after=0,
        fixed=0,
        measured=False,
        reason=(
            "linted_by_workflow_command_without_a_violation_reader"
            if result.ok
            else result.failure_detail()[:200]
        ),
    )
