from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from stress_stack.atomic import append_jsonl, atomic_write_json, atomic_write_text
from stress_stack.errors import ToolingError
from stress_stack.git_repository import GitRepository
from stress_stack.ingest import prepare_repository, update_stage, utc_now
from stress_stack.pytest_runner import PytestRunResult, compare, run_pytest
from stress_stack.source import resolve_source
from stress_stack.tooling import RUFF_VERSION, ensure_environment, ensure_ruff, install, run

_METADATA_DIRECTORY = ".stress_stack"
_CONFIGURATION_NAME = "ruff.toml"
_TEST_EXTRA_NAMES = frozenset({"test", "tests", "testing", "dev", "develop", "development"})
_STATISTIC_PATTERN = re.compile(r"^\s*(\d+)\s+(\S+)\s+(?:\[[^\]]*\]\s+)?(.*)$")


@dataclass(frozen=True, slots=True)
class LintResult:
    violations_before: int
    violations_after: int
    fixed: int
    residual_rules: dict[str, int]
    configuration_path: Path
    check_exit_code: int
    format_exit_code: int
    files_reformatted: int
    ruff_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ruff_version": self.ruff_version,
            "violations_before": self.violations_before,
            "violations_after_fix": self.violations_after,
            "violations_fixed": self.fixed,
            "residual_rules": dict(sorted(self.residual_rules.items())),
            "ignored_rule_count": len(self.residual_rules),
            "configuration": str(self.configuration_path),
            "files_reformatted": self.files_reformatted,
            "verification": {
                "ruff_check_exit_code": self.check_exit_code,
                "ruff_format_check_exit_code": self.format_exit_code,
                "lint_clean": self.check_exit_code == 0,
                "format_clean": self.format_exit_code == 0,
            },
        }


@dataclass(frozen=True, slots=True)
class HygieneResult:
    repository_root: Path
    metadata_root: Path
    action: str
    lint: LintResult
    tests_before: PytestRunResult
    tests_after: PytestRunResult
    comparison: dict[str, list[str]]
    status: str

    @property
    def regressions(self) -> list[str]:
        return self.comparison["regressions"]


def run_hygiene(source_value: str, *, cwd: Path | None = None) -> HygieneResult:
    working_directory = (cwd or Path.cwd()).resolve()
    source = resolve_source(source_value, working_directory)
    started_at = utc_now()
    start_clock = time.monotonic()

    repository, action = prepare_repository(source, working_directory)
    repository.validate()
    repository.add_metadata_exclude()

    metadata_root = repository.root / _METADATA_DIRECTORY
    hygiene_root = metadata_root / "hygiene"
    tools_root = metadata_root / "tools"
    hygiene_root.mkdir(parents=True, exist_ok=True)

    ruff = ensure_ruff(tools_root / "lint")
    _write_configuration(repository.root, _initial_configuration())

    environment = provision_test_environment(repository.root, tools_root / "test")
    python = Path(environment["python"])
    tests_before = _run_repository_tests(
        repository.root, python, environment, hygiene_root / "report_before.xml"
    )
    lint = _apply_lint(repository, ruff, hygiene_root)
    tests_after = _run_repository_tests(
        repository.root, python, environment, hygiene_root / "report_after.xml"
    )
    atomic_write_json(hygiene_root / "environment.json", environment)

    outcome = compare(tests_before, tests_after)
    status = _status(lint, tests_before, tests_after, outcome)
    completed_at = utc_now()

    atomic_write_json(hygiene_root / "lint.json", lint.to_dict())
    atomic_write_json(hygiene_root / "tests_before.json", tests_before.to_dict())
    atomic_write_json(hygiene_root / "tests_after.json", tests_after.to_dict())
    atomic_write_json(
        hygiene_root / "comparison.json",
        {
            "schema_version": "0.1.0",
            "generated_at": completed_at,
            "status": status,
            "unsafe_fixes_applied": False,
            "passing_before": len(tests_before.passing),
            "passing_after": len(tests_after.passing),
            **outcome,
        },
    )
    append_jsonl(
        metadata_root / "logs" / "hygiene.jsonl",
        {
            "event": "hygiene",
            "status": status,
            "started_at": started_at,
            "completed_at": completed_at,
            "duration_seconds": round(time.monotonic() - start_clock, 3),
            "repository_root": str(repository.root),
            "action": action,
            "ruff_version": RUFF_VERSION,
            "lint": lint.to_dict(),
            "tests_before": {
                "status": tests_before.status,
                "counts": tests_before.counts(),
            },
            "tests_after": {
                "status": tests_after.status,
                "counts": tests_after.counts(),
            },
            "test_extras": environment["installed_extras"],
            "regressions": outcome["regressions"],
        },
    )
    update_stage(metadata_root, "repo_hygiene", status)

    return HygieneResult(
        repository_root=repository.root,
        metadata_root=metadata_root,
        action=action,
        lint=lint,
        tests_before=tests_before,
        tests_after=tests_after,
        comparison=outcome,
        status=status,
    )


def _status(
    lint: LintResult,
    before: PytestRunResult,
    after: PytestRunResult,
    outcome: dict[str, list[str]],
) -> str:
    if outcome["regressions"]:
        return "regressed"
    if lint.check_exit_code != 0 or lint.format_exit_code != 0:
        return "not_clean"
    if before.status == "unavailable" or after.status == "unavailable":
        return "complete_unverified"
    if before.status == "no_tests":
        return "complete_no_tests"
    return "complete"


def _run_repository_tests(
    root: Path,
    python: Path,
    environment: dict[str, Any],
    report_path: Path,
) -> PytestRunResult:
    if not environment["installed"]:
        return PytestRunResult(
            status="unavailable",
            reason=environment["reason"],
            outcomes={},
            exit_code=None,
            duration_seconds=0.0,
        )
    return run_pytest(root, python, report_path)


_PROVISION_SCHEMA = "1"

# The files whose contents decide what gets installed. Deliberately not every
# `.py`: a source edit changes nothing about the environment, because every
# runtime path puts the source tree on PYTHONPATH ahead of site-packages. Hashing
# sources would also let any stage that writes into the tree invalidate the stamp,
# which is exactly the reinstall this exists to avoid.
_INSTALL_MANIFESTS = ("pyproject.toml", "setup.py", "setup.cfg", "tox.ini")


def _provision_fingerprint(root: Path, environment_root: Path) -> str:
    material: dict[str, Any] = {
        "schema": _PROVISION_SCHEMA,
        "interpreter": list(sys.version_info[:2]),
        "test_extras": sorted(_TEST_EXTRA_NAMES),
    }
    manifests = [root / name for name in _INSTALL_MANIFESTS]
    manifests += sorted(root.glob("requirements*.txt"))
    # The venv's own identity, without paying for a subprocess to ask it.
    manifests.append(environment_root / "pyvenv.cfg")
    for path in manifests:
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            digest = None
        material[path.name] = digest
    return hashlib.sha256(
        json.dumps(material, sort_keys=True).encode("utf-8")
    ).hexdigest()


def provision_test_environment(
    root: Path, environment_root: Path, *, force: bool = False
) -> dict[str, Any]:
    """Install the repository plus any test extras it declares.

    Extras are read from the installed distribution's own metadata rather than
    guessed, so a repository that declares its test dependencies (PyYAML, tomli,
    ...) under an extra gets them without any repository-specific knowledge.

    Idempotent, because it is called seven times per pipeline run and only the
    first call has anything to do. Each of the other six re-resolved the whole
    dependency set over the network — on glom that is the dominant term in the
    deps, coverage and container stages, all of which measure a few
    seconds of their own work. The stamp follows ``tooling.ensure_ruff``: check
    what is installed, skip if it is current.
    """
    python = ensure_environment(environment_root)
    stamp_path = environment_root / "provision_stamp.json"
    fingerprint = _provision_fingerprint(root, environment_root)
    if not force and python.exists() and stamp_path.is_file():
        try:
            stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stamp = {}
        if (
            stamp.get("schema") == _PROVISION_SCHEMA
            and stamp.get("fingerprint") == fingerprint
            and isinstance(stamp.get("record"), dict)
        ):
            return dict(stamp["record"])

    record: dict[str, Any] = {
        "python": str(python),
        "installed": False,
        "declared_extras": [],
        "declared_requirements": [],
        "requires_python": None,
        "project_name": None,
        "project_version": None,
        "installed_extras": [],
        "declared_groups": [],
        "installed_groups": [],
        "reason": None,
    }
    pytest_installation = install(python, ["pytest"])
    if not pytest_installation.ok:
        record["reason"] = f"pytest_install_failed: {pytest_installation.failure_detail()}"
        return record

    report = environment_root / "install_report.json"
    base = install(python, ["--report", str(report), str(root)])
    if not base.ok:
        record["reason"] = f"repository_install_failed: {base.failure_detail()}"
        return record
    record["installed"] = True

    declared = _declared_extras(report, root)
    record["declared_extras"] = declared
    record["declared_requirements"] = _declared_requirements(report, root)
    record["requirements_by_extra"] = _requirements_by_extra(report, root)
    entry = _local_install_entry(report, root)
    project_metadata = (entry or {}).get("metadata", {})
    record["requires_python"] = project_metadata.get("requires_python")
    record["project_name"] = project_metadata.get("name")
    record["project_version"] = project_metadata.get("version")
    wanted = [extra for extra in declared if extra.lower() in _TEST_EXTRA_NAMES]
    for extra in wanted:
        result = install(python, [f"{root}[{extra}]"])
        if result.ok:
            record["installed_extras"].append(extra)
        else:
            record["installed"] = False
            record["reason"] = f"test_extra_install_failed: {result.failure_detail()}"
            return record

    # PEP 735 dependency groups are intentionally not exposed as package
    # extras. Install the selected test group explicitly so the environment,
    # lockfile and container all describe the same suite dependencies.
    from stress_stack.locking import test_dependency_group

    group, requirements = test_dependency_group(root)
    if group:
        record["declared_groups"].append(group)
        record["requirements_by_group"] = {group: list(requirements)}
        record["declared_requirements"] = sorted(
            set(record["declared_requirements"]) | {_requirement_name(item) for item in requirements}
        )
        group_install = install(python, requirements)
        if not group_install.ok:
            record["installed"] = False
            record["reason"] = f"test_group_install_failed: {group_install.failure_detail()}"
            return record
        record["installed_groups"].append(group)

    # Only a complete install is stamped. Every failure path above returns
    # before this, so a broken environment is retried rather than remembered.
    atomic_write_json(
        stamp_path,
        {"schema": _PROVISION_SCHEMA, "fingerprint": fingerprint, "record": record},
    )
    return record


def _requirement_name(requirement: str) -> str:
    return re.split(r"[\s<>=!~;\[(]", requirement.strip(), maxsplit=1)[0]


def _declared_requirements(report: Path, root: Path) -> list[str]:
    """The project's own ``Requires-Dist`` names, without version specifiers."""
    entry = _local_install_entry(report, root)
    requires = (entry or {}).get("metadata", {}).get("requires_dist")
    if not isinstance(requires, list):
        return []
    names: list[str] = []
    for item in requires:
        if not isinstance(item, str):
            continue
        name = re.split(r"[\s<>=!~;\[(]", item.strip(), maxsplit=1)[0]
        if name:
            names.append(name)
    return sorted(set(names))


def _requirements_by_extra(report: Path, root: Path) -> dict[str, list[str]]:
    """Group declared requirements by the extra that gates them ("" = ungated).

    A requirement gated behind an extra we never installed cannot be checked
    against imports, so it must not be reported as unused.
    """
    entry = _local_install_entry(report, root)
    requires = (entry or {}).get("metadata", {}).get("requires_dist")
    grouped: dict[str, list[str]] = {}
    if not isinstance(requires, list):
        return grouped
    for item in requires:
        if not isinstance(item, str):
            continue
        name = re.split(r"[\s<>=!~;\[(]", item.strip(), maxsplit=1)[0]
        if not name:
            continue
        match = re.search(r'extra\s*==\s*[\'"]([^\'"]+)[\'"]', item)
        grouped.setdefault(match.group(1) if match else "", []).append(name)
    return {key: sorted(set(value)) for key, value in sorted(grouped.items())}


def _declared_extras(report: Path, root: Path) -> list[str]:
    entry = _local_install_entry(report, root)
    extras = (entry or {}).get("metadata", {}).get("provides_extra")
    if isinstance(extras, list):
        return [value for value in extras if isinstance(value, str)]
    return []


def _local_install_entry(report: Path, root: Path) -> dict[str, Any] | None:
    """The pip report entry for the repository itself, not its dependencies."""
    try:
        payload = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    resolved = root.resolve().as_uri()
    for item in payload.get("install", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        url = (item.get("download_info") or {}).get("url", "")
        if isinstance(url, str) and url.startswith(resolved):
            return item
    return None


def _apply_lint(repository: GitRepository, ruff: Path, hygiene_root: Path) -> LintResult:
    before = _violations(ruff, repository.root)
    _ruff(ruff, ["check", "--no-cache", "--fix", "."], repository.root)
    format_result = _ruff(ruff, ["format", "--no-cache", "."], repository.root)
    residual = _violations(ruff, repository.root)
    residual_rules = _rule_counts(residual)
    names = _rule_names(ruff, repository.root)

    _write_configuration(
        repository.root, _baseline_configuration(residual_rules, names, len(residual))
    )

    check = _ruff(ruff, ["check", "--no-cache", "."], repository.root)
    format_check = _ruff(ruff, ["format", "--no-cache", "--check", "."], repository.root)
    atomic_write_json(
        hygiene_root / "residual_violations.json",
        {
            "generated_at": utc_now(),
            "count": len(residual),
            "violations": [
                {
                    "code": item.get("code"),
                    "filename": _relative(item.get("filename"), repository.root),
                    "row": (item.get("location") or {}).get("row"),
                    "message": item.get("message"),
                }
                for item in residual
            ],
        },
    )
    return LintResult(
        violations_before=len(before),
        violations_after=len(residual),
        fixed=len(before) - len(residual),
        residual_rules=residual_rules,
        configuration_path=repository.root / _CONFIGURATION_NAME,
        check_exit_code=check.exit_code,
        format_exit_code=format_check.exit_code,
        files_reformatted=_reformatted_count(format_result.stdout),
        ruff_version=RUFF_VERSION,
    )


def _ruff(ruff: Path, arguments: list[str], root: Path):
    return run([str(ruff), *arguments], cwd=root, timeout=600.0)


def _violations(ruff: Path, root: Path) -> list[dict[str, Any]]:
    result = _ruff(ruff, ["check", "--no-cache", "--output-format", "json", "."], root)
    if result.exit_code not in {0, 1}:
        raise ToolingError(f"ruff check failed in {root}: {result.failure_detail()}")
    payload = result.stdout.strip()
    if not payload:
        return []
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ToolingError(f"Could not parse ruff output for {root}: {exc}") from exc
    return [item for item in parsed if isinstance(item, dict)]


def _rule_counts(violations: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in violations:
        code = item.get("code")
        if isinstance(code, str) and code:
            counts[code] = counts.get(code, 0) + 1
    return counts


def _rule_names(ruff: Path, root: Path) -> dict[str, str]:
    result = _ruff(ruff, ["check", "--no-cache", "--statistics", "."], root)
    names: dict[str, str] = {}
    for line in result.stdout.splitlines():
        match = _STATISTIC_PATTERN.match(line)
        if match and match.group(3).strip():
            names[match.group(2)] = match.group(3).strip()
    return names


def _relative(filename: Any, root: Path) -> str | None:
    if not isinstance(filename, str):
        return None
    try:
        return str(Path(filename).relative_to(root))
    except ValueError:
        return filename


def _reformatted_count(output: str) -> int:
    match = re.search(r"(\d+) files? reformatted", output)
    return int(match.group(1)) if match else 0


def _initial_configuration() -> str:
    return (
        "# Temporary ruff configuration written by stress-stack repo hygiene.\n"
        "# Replaced with the documented baseline once the fix pass completes.\n"
        "\n"
        f'extend-exclude = ["{_METADATA_DIRECTORY}"]\n'
    )


def _baseline_configuration(
    residual_rules: dict[str, int], names: dict[str, str], total: int
) -> str:
    adopted_on = date.today().isoformat()
    lines = [
        "# Ruff configuration generated by stress-stack repo hygiene.",
        "#",
        f"# Pinned tool:  ruff=={RUFF_VERSION}",
        "# Rule set:     ruff defaults (E4, E7, E9, F)",
        "# Applied:      `ruff check --fix` then `ruff format` (safe fixes only).",
        "#",
        f"# Baseline adopted {adopted_on}: {total} violation(s) across "
        f"{len(residual_rules)} rule(s)",
        "# remained after the fix pass. They are ignored below so `ruff check .`",
        "# exits 0 while every other rule stays enforced for new code.",
        "#",
        "# Unsafe fixes were deliberately NOT applied. `--unsafe-fixes` rewrites",
        "# code without preserving behaviour; on the sample repository it changed",
        "# test assertions and regressed previously-passing tests.",
    ]
    if residual_rules:
        lines.append("#")
        lines.append("# Residual counts at adoption:")
        for code, count in sorted(residual_rules.items(), key=lambda item: (-item[1], item[0])):
            label = names.get(code, "")
            suffix = f"  {label}" if label else ""
            lines.append(f"#   {code:<7} {count:>4}{suffix}")
    lines.append("")
    lines.append(f'extend-exclude = ["{_METADATA_DIRECTORY}"]')
    lines.append("")
    lines.append("[lint]")
    if not residual_rules:
        lines.append("ignore = []")
    else:
        lines.append("ignore = [")
        for code in sorted(residual_rules):
            label = names.get(code, "")
            comment = f"  # {label} ({residual_rules[code]})" if label else (
                f"  # {residual_rules[code]}"
            )
            lines.append(f'  "{code}",{comment}')
        lines.append("]")
    return "\n".join(lines) + "\n"


def _write_configuration(root: Path, content: str) -> None:
    path = root / _CONFIGURATION_NAME
    alternate = root / f".{_CONFIGURATION_NAME}"
    backup_root = root / _METADATA_DIRECTORY / "hygiene"
    for existing in (path, alternate):
        backup = backup_root / f"{existing.name}.original"
        if existing.exists() and not backup.exists() and not _is_generated(existing):
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(existing, backup)
    atomic_write_text(path, content)


def _is_generated(path: Path) -> bool:
    try:
        return path.read_text(encoding="utf-8").startswith(
            ("# Ruff configuration generated by stress-stack", "# Temporary ruff configuration")
        )
    except OSError:
        return False
