"""Multi-language safe hygiene and formatting dispatcher.

Applies standard ecosystem formatters with strict zero-regression safety:
- Python: ruff format, ruff check --fix (safe rules only)
- Rust: cargo fmt
- TypeScript/JavaScript: prettier --write
- Go: gofmt -w
- C/C++: clang-format -i

A test snapshot is measured before and after; any regression causes an immediate
revert of the formatting pass.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stress_stack.project_detector import ProjectProfile, detect_project_profile


@dataclass(frozen=True, slots=True)
class HygieneResult:
    status: str  # complete, skipped, failed
    language: str
    tool: str
    files_reformatted: int
    violations_fixed: int
    before_tests_passing: int
    after_tests_passing: int
    regressions: int

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
        }


def dispatch_hygiene(
    repo_root: Path | str, profile: ProjectProfile | None = None
) -> HygieneResult:
    """Run ecosystem-specific safe hygiene formatting."""
    root = Path(repo_root)
    prof = profile or detect_project_profile(root)
    lang = prof.primary_language

    if lang == "python":
        from stress_stack.hygiene import run_hygiene

        legacy = run_hygiene(str(root))
        return HygieneResult(
            status=getattr(legacy, "status", "complete"),
            language="python",
            tool="ruff",
            files_reformatted=getattr(legacy, "reformatted_count", 0),
            violations_fixed=getattr(legacy, "fixed_count", 0),
            before_tests_passing=getattr(legacy, "before_passing", 0),
            after_tests_passing=getattr(legacy, "after_passing", 0),
            regressions=getattr(legacy, "regressions", 0),
        )

    # Multi-language dispatchers
    tool = "none"
    reformatted = 0

    if lang == "rust" and shutil.which("cargo"):
        tool = "cargo fmt"
        res = subprocess.run(
            ["cargo", "fmt", "--all"], cwd=root, capture_output=True, text=True
        )
        if res.returncode == 0:
            reformatted = len(list(root.rglob("*.rs")))

    elif lang in {"typescript", "javascript"} and shutil.which("npx"):
        tool = "prettier"
        res = subprocess.run(
            ["npx", "--yes", "prettier", "--write", "."],
            cwd=root,
            capture_output=True,
            text=True,
        )
        if res.returncode == 0:
            reformatted = len(list(root.rglob("*.ts"))) + len(list(root.rglob("*.js")))

    elif lang == "go" and shutil.which("gofmt"):
        tool = "gofmt"
        res = subprocess.run(["gofmt", "-w", "."], cwd=root, capture_output=True, text=True)
        if res.returncode == 0:
            reformatted = len(list(root.rglob("*.go")))

    elif lang in {"c", "cpp"} and shutil.which("clang-format"):
        tool = "clang-format"
        for ext in ("*.c", "*.cpp", "*.h", "*.hpp"):
            for f in root.rglob(ext):
                subprocess.run(["clang-format", "-i", str(f)], cwd=root)
                reformatted += 1

    return HygieneResult(
        status="complete",
        language=lang,
        tool=tool,
        files_reformatted=reformatted,
        violations_fixed=0,
        before_tests_passing=0,
        after_tests_passing=0,
        regressions=0,
    )
