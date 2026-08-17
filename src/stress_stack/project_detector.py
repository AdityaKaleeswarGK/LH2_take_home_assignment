"""Project archetype, workspace, and ecosystem detector.

Scans the repository to identify:
1. Primary ecosystem (Python, Rust, TypeScript, Go, C++, Polyglot)
2. Build toolchain (cargo, pnpm, npm, yarn, uv, poetry, go, cmake)
3. Workspace layout (monorepo, multi-package, flat, src/)
4. Grounded test command & base container recipe (derived from project files + CI facts)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stress_stack.ci_parser import CIParsedFacts, parse_ci_facts


@dataclass
class ProjectProfile:
    root: Path
    primary_language: str
    languages_present: list[str]
    ecosystem: str
    toolchain: str
    is_monorepo: bool
    workspace_members: list[str]
    default_test_command: str
    pre_build_command: str | None
    base_image: str
    ci_facts: CIParsedFacts = field(default_factory=CIParsedFacts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary_language": self.primary_language,
            "languages_present": self.languages_present,
            "ecosystem": self.ecosystem,
            "toolchain": self.toolchain,
            "is_monorepo": self.is_monorepo,
            "workspace_members": self.workspace_members,
            "default_test_command": self.default_test_command,
            "pre_build_command": self.pre_build_command,
            "base_image": self.base_image,
            "ci_facts": self.ci_facts.to_dict(),
        }


def detect_project_profile(repo_root: Path | str) -> ProjectProfile:
    """Analyze repository files and CI workflows to construct a ProjectProfile."""
    root = Path(repo_root)
    ci_facts = parse_ci_facts(root)

    # Detect presence of manifests
    has_cargo = (root / "Cargo.toml").exists()
    has_package_json = (root / "package.json").exists()
    has_pnpm_workspace = (root / "pnpm-workspace.yaml").exists()
    has_go_mod = (root / "go.mod").exists()
    has_cmake = (root / "CMakeLists.txt").exists()
    has_pyproject = (root / "pyproject.toml").exists()
    has_setup_py = (root / "setup.py").exists()
    has_requirements = (root / "requirements.txt").exists()

    languages: list[str] = []
    if has_cargo:
        languages.append("rust")
    if has_package_json:
        languages.append("typescript" if any(root.rglob("tsconfig.json")) else "javascript")
    if has_go_mod:
        languages.append("go")
    if has_cmake:
        languages.append("cpp")
    if has_pyproject or has_setup_py or has_requirements:
        languages.append("python")

    # Count source files if no manifest found
    if not languages:
        for ext, lang in ((".py", "python"), (".rs", "rust"), (".ts", "typescript"), (".go", "go"), (".cpp", "cpp")):
            if any(root.glob(f"*{ext}")) or any((root / "src").glob(f"*{ext}")):
                languages.append(lang)

    primary = languages[0] if languages else "python"

    # Toolchain & Workspace detection
    toolchain = "unknown"
    ecosystem = primary
    is_monorepo = False
    workspace_members: list[str] = []
    pre_build = None
    default_test = "pytest -q"
    base_image = "python:3.12-slim"

    if primary == "rust":
        toolchain = "cargo"
        cargo_content = (root / "Cargo.toml").read_text(encoding="utf-8", errors="replace") if has_cargo else ""
        if "[workspace]" in cargo_content:
            is_monorepo = True
        default_test = "cargo test --all" if is_monorepo else "cargo test"
        base_image = "rust:1.78-slim"

    elif primary in {"typescript", "javascript"}:
        if (root / "pnpm-lock.yaml").exists() or has_pnpm_workspace:
            toolchain = "pnpm"
            is_monorepo = has_pnpm_workspace
        elif (root / "yarn.lock").exists():
            toolchain = "yarn"
        else:
            toolchain = "npm"

        default_test = "npm test"
        if has_package_json:
            try:
                pkg_data = json.loads((root / "package.json").read_text(encoding="utf-8"))
                if "build" in pkg_data.get("scripts", {}):
                    pre_build = f"{toolchain} run build"
                if "test" in pkg_data.get("scripts", {}):
                    default_test = f"{toolchain} test"
            except Exception:
                pass
        base_image = "node:20-slim"

    elif primary == "go":
        toolchain = "go"
        is_monorepo = (root / "go.work").exists()
        default_test = "go test ./..."
        base_image = "golang:1.22-alpine"

    elif primary == "cpp":
        toolchain = "cmake"
        default_test = "ctest --output-on-failure"
        pre_build = "cmake -B build && cmake --build build"
        base_image = "debian:bookworm-slim"

    else:  # python
        if (root / "poetry.lock").exists():
            toolchain = "poetry"
            default_test = "poetry run pytest -q"
        elif (root / "Pipfile").exists():
            toolchain = "pipenv"
            default_test = "pipenv run pytest -q"
        else:
            toolchain = "uv"
            default_test = "python -m pytest -q"
        base_image = "python:3.12.9-slim"

    # If CI facts found a verified test command, prefer it
    if ci_facts.test_commands:
        default_test = ci_facts.test_commands[0]

    return ProjectProfile(
        root=root,
        primary_language=primary,
        languages_present=languages,
        ecosystem=ecosystem,
        toolchain=toolchain,
        is_monorepo=is_monorepo,
        workspace_members=workspace_members,
        default_test_command=default_test,
        pre_build_command=pre_build,
        base_image=base_image,
        ci_facts=ci_facts,
    )
