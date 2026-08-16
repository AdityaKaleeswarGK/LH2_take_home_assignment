from __future__ import annotations

import argparse
import sys
from pathlib import Path

from stress_stack import __version__
from stress_stack.config import KNOWN_MODELS, config_path, load_settings, save_settings
from stress_stack.errors import StressStackError
from stress_stack.graph import (
    DependencyArtifacts,
    GraphArtifacts,
    build_container_artifacts,
    build_coverage_artifacts,
    build_enrichment_artifacts,
    build_dependency_artifacts,
    build_emission_artifacts,
    build_graph_artifacts,
    build_index_artifacts,
    build_mining_artifacts,
    build_selection_artifacts,
    build_validation_artifacts,
)
from stress_stack.hygiene import HygieneResult, run_hygiene
from stress_stack.pipeline import PipelineResult, run_pipeline
from stress_stack.ingest import ingest

_SOURCE_HELP = (
    "Public GitHub URL or path to an existing local Git repository "
    "(default: current directory)"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stress-stack",
        description="Build repository-local Git history and apply repository hygiene.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    ingest_parser = subparsers.add_parser(
        "ingest", help="Clone or analyze a repository and extract its history"
    )
    ingest_parser.add_argument("source", nargs="?", default=".", help=_SOURCE_HELP)
    hygiene_parser = subparsers.add_parser(
        "hygiene",
        help="Apply ruff lint and format hygiene, verified by a before/after test run",
    )
    hygiene_parser.add_argument("source", nargs="?", default=".", help=_SOURCE_HELP)
    graph_parser = subparsers.add_parser(
        "graph",
        help="Build the verified repository symbol graph from Python source",
    )
    graph_parser.add_argument("source", nargs="?", default=".", help=_SOURCE_HELP)
    deps_parser = subparsers.add_parser(
        "deps",
        help="Classify imports and pin third-party dependencies to exact versions",
    )
    deps_parser.add_argument("source", nargs="?", default=".", help=_SOURCE_HELP)
    container_parser = subparsers.add_parser(
        "container",
        help="Generate a Dockerfile and verify it builds and runs the suite twice identically",
    )
    container_parser.add_argument("source", nargs="?", default=".", help=_SOURCE_HELP)
    coverage_parser = subparsers.add_parser(
        "coverage",
        help="Measure per-test coverage and attribute covered lines to symbols",
    )
    coverage_parser.add_argument("source", nargs="?", default=".", help=_SOURCE_HELP)
    index_parser = subparsers.add_parser(
        "index",
        help="Project the knowledge artifacts into a queryable SQLite index",
    )
    index_parser.add_argument("source", nargs="?", default=".", help=_SOURCE_HELP)
    mine_parser = subparsers.add_parser(
        "mine",
        help="Rank history-derived and excision task candidates from measured evidence",
    )
    mine_parser.add_argument("source", nargs="?", default=".", help=_SOURCE_HELP)
    validate_parser = subparsers.add_parser(
        "validate",
        help="Stage ranked candidates as tasks and run every validation gate",
    )
    validate_parser.add_argument("source", nargs="?", default=".", help=_SOURCE_HELP)
    validate_parser.add_argument("--history-limit", type=int, default=12)
    validate_parser.add_argument("--excision-limit", type=int, default=8)
    validate_parser.add_argument(
        "--repeats", type=int, default=2, help="Fresh repeats per side for the determinism gate"
    )
    validate_parser.add_argument("--only", choices=("history", "excision"), default=None)
    validate_parser.add_argument(
        "--import-policy", choices=("strict", "measured"), default="strict",
        help="Whether a verifier that could not be collected before the change "
             "counts as failing (default: strict, it does not)",
    )
    run_parser = subparsers.add_parser(
        "run",
        help="Run every stage in dependency order and emit the task bundle",
    )
    run_parser.add_argument("source", nargs="?", default=".", help=_SOURCE_HELP)
    run_parser.add_argument("--history-limit", type=int, default=30)
    run_parser.add_argument("--excision-limit", type=int, default=12)
    run_parser.add_argument("--repeats", type=int, default=2)
    run_parser.add_argument(
        "--skip", action="append", default=[], metavar="STAGE",
        help="Skip a stage by name; repeatable (e.g. --skip enrich)",
    )
    select_parser = subparsers.add_parser(
        "select",
        help="Choose the final ten under the brief's quotas and record compliance",
    )
    select_parser.add_argument("source", nargs="?", default=".", help=_SOURCE_HELP)
    emit_parser = subparsers.add_parser(
        "emit",
        help="Write each task's instruction and manifest, then tasks.json",
    )
    emit_parser.add_argument("source", nargs="?", default=".", help=_SOURCE_HELP)
    enrich_parser = subparsers.add_parser(
        "enrich", help="Build coverage, per-file semantic cards, and the repository blueprint"
    )
    enrich_parser.add_argument("source", nargs="?", default=".", help=_SOURCE_HELP)
    enrich_parser.add_argument("--workers", type=int, default=20)
    model_parser = subparsers.add_parser(
        "model", help="Configure the OpenRouter key and per-role model assignments"
    )
    model_parser.add_argument(
        "--set-key", action="store_true", help="Prompt for an API key without echoing it"
    )
    model_parser.add_argument("--key-stdin", action="store_true", help="Read the key from stdin")
    model_parser.add_argument("--clear-key", action="store_true")
    model_parser.add_argument(
        "--set", dest="assignments", action="append", metavar="ROLE=MODEL",
        help="Assign a model to a role, e.g. --set worker=openai/gpt-5.6-luna",
    )
    return parser


def main(arguments: list[str] | None = None) -> int:
    parser = build_parser()
    namespace = parser.parse_args(arguments)
    try:
        if namespace.command == "ingest":
            result = ingest(namespace.source, cwd=Path.cwd())
        elif namespace.command == "hygiene":
            return _print_hygiene_result(run_hygiene(namespace.source, cwd=Path.cwd()))
        elif namespace.command == "graph":
            return _print_graph_result(build_graph_artifacts(namespace.source, cwd=Path.cwd()))
        elif namespace.command == "container":
            return _print_container_result(
                build_container_artifacts(namespace.source, cwd=Path.cwd())
            )
        elif namespace.command == "coverage":
            return _print_coverage_result(
                build_coverage_artifacts(namespace.source, cwd=Path.cwd())
            )
        elif namespace.command == "index":
            return _print_index_result(build_index_artifacts(namespace.source, cwd=Path.cwd()))
        elif namespace.command == "mine":
            return _print_mining_result(build_mining_artifacts(namespace.source, cwd=Path.cwd()))
        elif namespace.command == "validate":
            return _print_validation_result(
                build_validation_artifacts(
                    namespace.source,
                    cwd=Path.cwd(),
                    history_limit=namespace.history_limit,
                    excision_limit=namespace.excision_limit,
                    repeats=namespace.repeats,
                    only=namespace.only,
                    policy=namespace.import_policy,
                )
            )
        elif namespace.command == "run":
            return _print_pipeline(
                run_pipeline(
                    namespace.source,
                    cwd=Path.cwd(),
                    history_limit=namespace.history_limit,
                    excision_limit=namespace.excision_limit,
                    repeats=namespace.repeats,
                    skip=tuple(namespace.skip),
                )
            )
        elif namespace.command == "select":
            return _print_selection(build_selection_artifacts(namespace.source, cwd=Path.cwd()))
        elif namespace.command == "emit":
            return _print_emission(build_emission_artifacts(namespace.source, cwd=Path.cwd()))
        elif namespace.command == "enrich":
            return _print_enrichment(
                build_enrichment_artifacts(
                    namespace.source, cwd=Path.cwd(), workers=namespace.workers
                )
            )
        elif namespace.command == "model":
            return _configure_model(namespace)
        elif namespace.command == "deps":
            return _print_dependency_result(
                build_dependency_artifacts(namespace.source, cwd=Path.cwd())
            )
        else:
            parser.error(f"Unsupported command: {namespace.command}")
    except StressStackError as exc:
        print(f"stress-stack: error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("stress-stack: interrupted", file=sys.stderr)
        return 130

    print(f"Repository: {result.repository_root}")
    print(f"Action: {result.action}")
    print(f"HEAD: {result.head_sha}")
    print(f"Commits: {result.commit_count}")
    print(f"Pull requests: {result.pull_request_count} ({result.pull_request_status})")
    print(f"Metadata: {result.metadata_root}")
    return 0


def _print_hygiene_result(result: HygieneResult) -> int:
    lint = result.lint
    print(f"Repository: {result.repository_root}")
    print(f"Action: {result.action}")
    print(f"Ruff: {lint.ruff_version} (safe fixes only)")
    print(
        f"Lint: {lint.violations_before} -> {lint.violations_after} "
        f"({lint.fixed} fixed, {len(lint.residual_rules)} rules baselined)"
    )
    print(f"Format: {lint.files_reformatted} files reformatted")
    print(f"Config: {lint.configuration_path}")
    print(f"Tests before: {_test_summary(result.tests_before)}")
    print(f"Tests after:  {_test_summary(result.tests_after)}")
    print(f"Regressions: {len(result.regressions)}")
    for node_id in result.regressions[:10]:
        print(f"  - {node_id}")
    print(f"Status: {result.status}")
    print(f"Evidence: {result.metadata_root / 'hygiene'}")
    return 0 if result.status != "regressed" else 1


def _print_graph_result(result: GraphArtifacts) -> int:
    statistics = result.statistics
    validation = result.validation
    print(f"Repository: {result.repository_root}")
    print(f"Action: {result.action}")
    print(
        f"Files: {statistics['files']} ({statistics['test_files']} test, "
        f"{statistics['syntax_errors']} unparsable)"
    )
    print(f"Symbols: {statistics['symbols']}")
    print(
        "Edges: "
        + ", ".join(
            f"{kind} {statistics[f'edges_{kind}']}"
            for kind in ("contains", "imports", "calls", "inherits")
        )
        + f" (total {statistics['edges']})"
    )
    print(f"Unresolved: {statistics['unresolved']}")
    print(f"External modules: {statistics['external_modules']}")
    print(
        f"Validation: {validation['status']} "
        f"(edges {validation['edge_match_rate']:.4f}, anchors {validation['anchor_match_rate']:.4f})"
    )
    print(f"Knowledge: {result.knowledge_root}")
    return 0 if validation["status"] == "verified" else 1


def _print_dependency_result(result: DependencyArtifacts) -> int:
    counts = result.counts
    print(f"Repository: {result.repository_root}")
    print(
        "Imports: "
        + ", ".join(
            f"{kind} {counts.get(kind, 0)}"
            for kind in ("internal", "stdlib", "third_party")
        )
    )
    print(
        "Direct third-party imports by role: "
        + f"runtime {len(result.runtime)}, test {len(result.test)}, "
        + f"optional {len(result.optional)}"
    )
    lock = result.lock
    print(
        f"Lock: {lock['status']} from {lock['manifest']} "
        f"extras={lock['extras'] or 'none'} "
        f"-> {lock['package_count']} packages"
        + (" with hashes" if lock["hashed"] else "")
    )
    if lock["reason"]:
        print(f"  note: {lock['reason']}")
    audit = result.audit
    print(f"Imported but not declared: {audit['imported_not_declared'] or 'none'}")
    print(f"Declared but not imported: {audit['declared_not_imported'] or 'none'}")
    print(f"Imported but not in lock: {audit['imported_not_locked'] or 'none'}")
    if audit.get("declared_in_uninstalled_extra"):
        print(
            "Unverifiable (declared in an extra we did not install): "
            f"{audit['declared_in_uninstalled_extra']}"
        )
    print(f"Lockfile: {result.lockfile}")
    if not result.environment_available:
        print("Warning: no provisioned environment; import roles could not be resolved")
        return 1
    return 0 if lock["status"].startswith("locked") else 1


def _print_coverage_result(report: dict) -> int:
    print(f"Repository: {report['repository_root']}")
    print(f"Coverage: {report['coverage']}")
    statistics = report["statistics"]
    if statistics:
        print(
            f"  {statistics['symbols_covered']}/{statistics['symbols_total']} symbols covered, "
            f"{statistics['tests_observed']} tests, "
            f"{statistics['excision_candidates']} excision candidates"
        )
    print(f"Knowledge: {report['knowledge_root']}")
    return 0 if report["coverage"] == "available" else 1


def _print_index_result(report: dict) -> int:
    print(f"Repository: {report['repository_root']}")
    print(
        f"Indexed: {report['files']} files, {report['symbols']} symbols, "
        f"{report['edges']} edges, {report['pull_requests']} prs"
    )
    print(f"Coverage: {report['tests']} tests, {report['coverage_rows']} test-symbol rows")
    matrix = report["module_test_matrix"]
    print(f"Modules exercised by tests: {len(matrix)}")
    for module, count in list(matrix.items())[:10]:
        print(f"  {module:<28} {count:>4} tests")
    print(f"Index: {report['path']}")
    return 0


def _print_mining_result(report: dict) -> int:
    print(f"Repository: {report['repository_root']}")
    print(f"Coverage: {report['coverage_status']}")
    print(f"Churn distribution: {report['thresholds']['churn_distribution']}")
    for name in ("history", "excision"):
        funnel = report[f"{name}_funnel"]
        print(
            f"{name.capitalize()}: {report[f'{name}_count']} ranked "
            f"of {funnel['considered']} considered"
        )
        for reason, count in funnel["dropped_counts"].items():
            print(f"  dropped {count:>4}  {reason}")
        modules = report[f"{name}_modules"]
        print(f"  modules ({len(modules)}): " + ", ".join(
            f"{module} {count}" for module, count in list(modules.items())[:8]
        ))
    print(f"Knowledge: {report['knowledge_root']}")
    return 0


def _print_validation_result(report: dict) -> int:
    print(f"Repository: {report['repository_root']}")
    print(f"Runner: {report['backend']} ({report['image']})")
    print(f"Import policy: {report['policy']}")
    for name, entry in report["by_source"].items():
        print(
            f"{name.capitalize()}: {entry['eligible']} eligible of {entry['attempted']} "
            f"attempted in {entry['seconds']}s"
        )
        for reason, count in entry["rejected_counts"].items():
            print(f"  rejected {count:>3}  {reason}")
    summary = report["summary"]
    print(f"Total eligible: {summary['eligible']} of {summary['attempted']}")
    for task_id in report["eligible"]:
        print(f"  + {task_id}")
    print(f"Tasks: {report['tasks_root']}")
    print(f"Evidence: {report['knowledge_root']}/validation.json")
    return 0 if summary["eligible"] else 1


def _print_pipeline(result: PipelineResult) -> int:
    print(f"Repository: {result.repository_root or '(not resolved)'}")
    for stage in result.stages:
        line = f"  {stage.name:<10} {stage.status:<9} {stage.seconds:>7.1f}s"
        print(line if not stage.detail else f"{line}  {stage.detail.splitlines()[0][:90]}")
    manifest = result.manifest
    if manifest:
        print(f"Tasks: {manifest.get('task_count')} {manifest.get('by_source')}")
        print(
            f"Modules: {manifest.get('distinct_modules')} | "
            f"difficulty {manifest.get('difficulty_spread')} | "
            f"quota satisfied {manifest.get('quota_satisfied')}"
        )
        print(f"Leaking instructions: {manifest.get('instructions_leaking') or 'none'}")
    print(f"Pipeline: {'ok' if result.ok else 'failed'}")
    return 0 if result.ok and manifest.get("quota_satisfied") else 1


def _print_selection(report: dict) -> int:
    ledger = report["ledger"]
    print(f"Repository: {report['repository_root']}")
    print(f"Pool: {report['pool_size']} eligible -> {ledger['selected']} selected")
    print(f"By source: {ledger['by_source']}")
    print(f"Modules covered: {ledger['distinct_modules']} {ledger['modules_covered']}")
    tiers: dict[str, int] = {}
    for entry in report["difficulty"].values():
        tiers[entry["tier"]] = tiers.get(entry["tier"], 0) + 1
    print(f"Difficulty: {dict(sorted(tiers.items()))}")
    for entry in ledger["entries"]:
        print(f"  {entry['source']:<9} {entry['module']:<24} {entry['task_id']}")
    print(f"Quota satisfied: {ledger['satisfied']}")
    for problem in ledger["shortfalls"]:
        print(f"  shortfall: {problem}")
    return 0 if ledger["satisfied"] else 1


def _print_emission(report: dict) -> int:
    print(f"Repository: {report['repository_root']}")
    print(f"Tasks written: {report['task_count']}")
    print(f"By source: {report['by_source']}")
    print(f"Modules covered: {report['distinct_modules']} {report['modules_covered']}")
    print(f"Difficulty: {report['difficulty_spread']}")
    print(f"Quota satisfied: {report['quota_satisfied']}")
    for problem in report["shortfalls"]:
        print(f"  shortfall: {problem}")
    print(f"Instructions failing the leak check: {report['instructions_leaking'] or 'none'}")
    print(f"Manifest: {report['manifest']}")
    return 0 if report["quota_satisfied"] and not report["instructions_leaking"] else 1


def _print_enrichment(report: dict) -> int:
    print(f"Coverage: {report['coverage']}")
    if report["coverage_statistics"]:
        stats = report["coverage_statistics"]
        print(
            f"  {stats['symbols_covered']}/{stats['symbols_total']} symbols covered, "
            f"{stats['tests_observed']} tests, "
            f"{stats['excision_candidates']} excision candidates"
        )
    cards = report["cards"]
    print(f"File cards: {cards['by_status']}")
    print(f"  grounded {cards['grounded']}/{cards['files']}, "
          f"mean entity coverage {cards['mean_entity_coverage']:.0%}")
    blueprint = report["blueprint"]
    print(f"Blueprint: {blueprint.get('status')} "
          f"({blueprint.get('files_cited', 0)} files cited, "
          f"unknown {blueprint.get('unknown_files') or 'none'})")
    usage = report["usage"]
    print(f"Model usage: {usage['live_calls']} live + {usage['cache_hits']} cached, "
          f"{usage['total_tokens']} tokens, ${usage['cost_usd']}")
    print(f"Knowledge: {report['knowledge_root']}")
    return 0


def _configure_model(namespace) -> int:
    import getpass

    api_key = None
    if namespace.set_key:
        api_key = getpass.getpass("OpenRouter API key: ")
    elif namespace.key_stdin:
        api_key = sys.stdin.readline().strip()

    assignments: dict[str, str] = {}
    for item in namespace.assignments or []:
        role, separator, model = item.partition("=")
        if not separator:
            print(f"stress-stack: error: expected ROLE=MODEL, got {item!r}", file=sys.stderr)
            return 1
        assignments[role.strip()] = model.strip()

    changed = bool(api_key or namespace.clear_key or assignments)
    settings = (
        save_settings(api_key=api_key, clear_key=namespace.clear_key, models=assignments)
        if changed
        else load_settings()
    )
    print(f"Endpoint: {settings.endpoint}")
    print(f"API key: {'configured' if settings.configured else 'not configured'}")
    print(f"Key source: {settings.key_source or 'none'}")
    print("Models by role:")
    for role, model in sorted(settings.models.items()):
        print(f"  {role:10} {model}")
    print(f"Known models: {', '.join(KNOWN_MODELS)}")
    print(f"Config: {config_path()}")
    return 0


def _print_container_result(result) -> int:
    print(f"Dockerfile: {result.dockerfile}")
    print(f"Python: {result.python_version}")
    print(f"Base image: {result.base_image}")
    print(f"Build: {result.build_status} in {result.build_seconds}s")
    if result.build_status != "built":
        print(result.build_log_tail)
        print(f"Status: {result.status}")
        return 1
    for entry in result.runs:
        print(
            f"  {entry['name']}: {entry['passed']}/{entry['total']} passed "
            f"(exit {entry['exit_code']}, {entry['seconds']}s)"
        )
    print(f"Runs identical: {result.identical}")
    print(f"Matches host baseline: {result.baseline_match}")
    print(f"Status: {result.status}")
    return 0 if result.status == "verified" else 1


def _test_summary(result) -> str:
    if result.status != "ran":
        return f"{result.status}" + (f" ({result.reason})" if result.reason else "")
    counts = result.counts()
    parts = ", ".join(f"{value} {name}" for name, value in sorted(counts.items()))
    return f"{len(result.passing)} passing of {len(result.outcomes)} ({parts})"
