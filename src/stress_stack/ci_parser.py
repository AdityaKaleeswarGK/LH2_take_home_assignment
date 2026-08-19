"""CI workflow and task runner parser.

Extracts ground-truth execution facts directly from the project's own CI:
- .github/workflows/*.yml
- Makefile / Justfile / Taskfile.yml
- tox.ini / noxfile.py
- package.json scripts

This guarantees that the container and test runner reflect the project's actual
recipe rather than guessing from language alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CIParsedFacts:
    test_commands: list[str] = field(default_factory=list)
    build_commands: list[str] = field(default_factory=list)
    setup_commands: list[str] = field(default_factory=list)
    environment_variables: dict[str, str] = field(default_factory=dict)
    system_packages: list[str] = field(default_factory=list)
    matrix_versions: list[str] = field(default_factory=list)
    source_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_commands": self.test_commands,
            "build_commands": self.build_commands,
            "setup_commands": self.setup_commands,
            "environment_variables": self.environment_variables,
            "system_packages": self.system_packages,
            "matrix_versions": self.matrix_versions,
            "source_files": self.source_files,
        }


_APT_INSTALL_RE = re.compile(
    r"""apt-get\s+install\s+(?:-[yq\s]+)?([a-zA-Z0-9_\-.\s]+)"""
)
_MAKE_TEST_TARGET_RE = re.compile(
    r"""^(test|check|test-all|unit-test|tests):\s*.*$""", re.MULTILINE
)


def parse_ci_facts(root: Path) -> CIParsedFacts:
    """Extract build, test, and environment facts from project CI files."""
    facts = CIParsedFacts()

    # 1. Parse GitHub Actions Workflows
    workflow_dir = root / ".github" / "workflows"
    if workflow_dir.is_dir():
        for yml_file in workflow_dir.glob("*.y*ml"):
            try:
                content = yml_file.read_text(encoding="utf-8", errors="replace")
                facts.source_files.append(str(yml_file.relative_to(root)))
                _extract_workflow_facts(content, facts)
            except Exception:
                continue

    # 2. Parse Makefile / Justfile
    for makefile_name in ("Makefile", "makefile", "Justfile", "Taskfile.yml"):
        make_path = root / makefile_name
        if make_path.is_file():
            try:
                content = make_path.read_text(encoding="utf-8", errors="replace")
                facts.source_files.append(makefile_name)
                _extract_makefile_facts(content, facts, makefile_name)
            except Exception:
                continue

    # 3. Parse package.json test script (for JS/TS)
    pkg_json = root / "package.json"
    if pkg_json.is_file():
        try:
            import json

            data = json.loads(pkg_json.read_text(encoding="utf-8"))
            scripts = data.get("scripts", {})
            if "test" in scripts:
                # `npm run test`, not `npm run test -- <body>`. The `--` form
                # passes the script's own body back to itself as arguments, so a
                # `"test": "vitest run"` script became `vitest run vitest run`
                # and the two extra words were read as filename filters —
                # matching nothing, exiting non-zero, and failing the container
                # stage for a suite that passes. Only visible once TypeScript
                # got far enough to reach that stage.
                facts.test_commands.append("npm run test")
            if "build" in scripts:
                facts.build_commands.append("npm run build")
            facts.source_files.append("package.json")
        except Exception:
            pass

    return facts


_VERSION_KEYS = ("python-version:", "node-version:", "rust:", "go-version:")
_VERSION_RE = re.compile(r"""['"]?(\d+(?:\.\d+)*)['"]?""")
_LIST_ITEM_RE = re.compile(r"^-\s*['\"]?(\d+(?:\.\d+)*)['\"]?\s*$")


def _extract_workflow_facts(content: str, facts: CIParsedFacts) -> None:
    # A matrix is the most concrete statement a project makes about which
    # toolchains it actually runs on, and it is written two ways: inline
    # (`python-version: ["3.9", "3.10"]`) and as an indented block list. Reading
    # only the first match on the key line saw one version of either, which made
    # a matrix indistinguishable from a single pinned version.
    version_key_indent: int | None = None

    for line in content.splitlines():
        stripped = line.strip()

        if version_key_indent is not None:
            item = _LIST_ITEM_RE.match(stripped)
            indent = len(line) - len(line.lstrip())
            if item and indent > version_key_indent:
                facts.matrix_versions.append(item.group(1))
                continue
            if stripped and not stripped.startswith("#"):
                version_key_indent = None
        # Match "run: ...", "- run: ...", or "run: |"
        if stripped.startswith("- run:"):
            cmd = stripped[6:].strip().strip('"').strip("'")
            if cmd:
                _categorize_command(cmd, facts)
        elif stripped.startswith("run:"):
            cmd = stripped[4:].strip().strip('"').strip("'")
            if cmd:
                _categorize_command(cmd, facts)

        # Look for system apt packages
        for match in _APT_INSTALL_RE.finditer(line):
            pkgs = match.group(1).split()
            facts.system_packages.extend(p for p in pkgs if not p.startswith("-"))

        # Look for node/python/rust/go versions in matrix
        matched_key = next((key for key in _VERSION_KEYS if key in line), None)
        if matched_key:
            after = line.split(matched_key, 1)[1]
            found = _VERSION_RE.findall(after)
            facts.matrix_versions.extend(found)
            # An empty tail means the versions are on the following lines.
            version_key_indent = None if found else len(line) - len(line.lstrip())


def _extract_makefile_facts(content: str, facts: CIParsedFacts, filename: str) -> None:
    if _MAKE_TEST_TARGET_RE.search(content):
        facts.test_commands.append(f"make -f {filename} test")


def _categorize_command(cmd: str, facts: CIParsedFacts) -> None:
    lower = cmd.lower()
    if any(k in lower for k in ("pytest", "cargo test", "go test", "npm test", "vitest", "jest", "ctest", "make test")):
        if cmd not in facts.test_commands:
            facts.test_commands.append(cmd)
    elif any(k in lower for k in ("build", "cargo build", "npm run build", "make", "setup.py build_ext")):
        if cmd not in facts.build_commands:
            facts.build_commands.append(cmd)
    elif any(k in lower for k in ("pip install", "npm install", "cargo fetch", "go mod download")):
        if cmd not in facts.setup_commands:
            facts.setup_commands.append(cmd)
