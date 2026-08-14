from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from inverse_alpha import __version__
from inverse_alpha.config import (
    config_path,
    load_openrouter_settings,
    save_openrouter_settings,
)
from inverse_alpha.errors import InputError, InverseAlphaError
from inverse_alpha.ingest import ingest
from inverse_alpha.knowledge import KnowledgeResult, build_knowledge


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inverse-alpha",
        description="Build repository-local history and deterministic Python knowledge.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    ingest_parser = subparsers.add_parser(
        "ingest", help="Clone or analyze a repository and extract its history"
    )
    ingest_parser.add_argument(
        "source", help="Public GitHub URL or path to an existing local Git repository"
    )
    knowledge_parser = subparsers.add_parser(
        "knowledge", help="Build a verified Python repository graph and OKF bundle"
    )
    knowledge_parser.add_argument(
        "source", help="Public GitHub URL or path to an existing local Git repository"
    )
    _add_enrichment_arguments(knowledge_parser)
    complete_parser = subparsers.add_parser(
        "complete",
        aliases=["completely"],
        help="Run history ingestion followed by the Python knowledge pipeline",
    )
    complete_parser.add_argument(
        "source",
        nargs="?",
        default=".",
        help="GitHub URL or local repository path (default: current directory)",
    )
    _add_enrichment_arguments(complete_parser)
    config_parser = subparsers.add_parser(
        "config",
        help="Configure OpenRouter without storing credentials in a repository",
    )
    config_parser.add_argument(
        "--set-key",
        action="store_true",
        help="Securely prompt for an OpenRouter API key",
    )
    config_parser.add_argument(
        "--api-key-stdin",
        action="store_true",
        help="Read an OpenRouter API key from standard input",
    )
    config_parser.add_argument("--clear-key", action="store_true")
    config_parser.add_argument("--model", help="Set the default OpenRouter model")
    config_parser.add_argument(
        "--endpoint", help="Override the OpenRouter API endpoint"
    )
    return parser


def main(arguments: list[str] | None = None) -> int:
    parser = build_parser()
    namespace = parser.parse_args(arguments)
    try:
        if namespace.command == "config":
            return _configure_openrouter(namespace)
        if namespace.command == "ingest":
            result = ingest(namespace.source, cwd=Path.cwd())
        elif namespace.command in {"knowledge", "complete", "completely"}:
            knowledge_result = build_knowledge(
                namespace.source,
                cwd=Path.cwd(),
                enrichment_mode=namespace.enrichment,
                enrichment_model=namespace.model,
                refresh_enrichment=namespace.refresh_llm,
            )
            _print_knowledge_result(knowledge_result)
            return 0
        else:
            parser.error(f"Unsupported command: {namespace.command}")
    except InverseAlphaError as exc:
        print(f"inverse-alpha: error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("inverse-alpha: interrupted", file=sys.stderr)
        return 130

    print(f"Repository: {result.repository_root}")
    print(f"Action: {result.action}")
    print(f"HEAD: {result.head_sha}")
    print(f"Commits: {result.commit_count}")
    print(f"Pull requests: {result.pull_request_count} ({result.pull_request_status})")
    print(f"Metadata: {result.metadata_root}")
    return 0


def _print_knowledge_result(result: KnowledgeResult) -> None:
    print(f"Repository: {result.repository_root}")
    print(f"Action: {result.action}")
    print(f"HEAD: {result.head_sha}")
    print(f"Source digest: {result.source_digest}")
    print(f"Files: {result.file_count}")
    print(f"Symbols: {result.symbol_count}")
    print(f"Edges: {result.edge_count}")
    print(f"Unresolved references: {result.unresolved_count}")
    print(f"Validation: {result.validation_status}")
    enrichment = result.enrichment_status
    if result.enrichment_provider:
        enrichment += f" ({result.enrichment_provider}/{result.enrichment_model})"
    print(f"Enrichment: {enrichment}")
    print(f"Annotations: {result.annotation_count}")
    print(f"Knowledge: {result.knowledge_root}")
    print(f"Blueprint: {result.knowledge_root / 'blueprint.md'}")
    print(f"Test map: {result.knowledge_root / 'test_map.md'}")


def _add_enrichment_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--enrichment",
        choices=("auto", "openrouter", "none"),
        default="auto",
        help="Semantic enrichment mode (default: auto; uses OpenRouter when configured)",
    )
    parser.add_argument(
        "--llm",
        dest="enrichment",
        action="store_const",
        const="openrouter",
        help="Require OpenRouter semantic enrichment",
    )
    parser.add_argument(
        "--no-llm",
        dest="enrichment",
        action="store_const",
        const="none",
        help="Disable semantic enrichment for this run",
    )
    parser.add_argument("--model", help="Override the configured OpenRouter model")
    parser.add_argument(
        "--refresh-llm",
        action="store_true",
        help="Ignore semantic caches and request fresh OpenRouter annotations",
    )


def _configure_openrouter(namespace: argparse.Namespace) -> int:
    if namespace.set_key and namespace.api_key_stdin:
        raise InputError("Choose either --set-key or --api-key-stdin, not both")
    api_key: str | None = None
    if namespace.set_key:
        api_key = getpass.getpass("OpenRouter API key: ")
    elif namespace.api_key_stdin:
        api_key = sys.stdin.readline().strip()
    changed = bool(
        namespace.set_key
        or namespace.api_key_stdin
        or namespace.clear_key
        or namespace.model
        or namespace.endpoint
    )
    settings = (
        save_openrouter_settings(
            api_key=api_key,
            model=namespace.model,
            endpoint=namespace.endpoint,
            clear_key=namespace.clear_key,
        )
        if changed
        else load_openrouter_settings()
    )
    print("Provider: openrouter")
    print(f"Model: {settings.model}")
    print(f"Endpoint: {settings.endpoint}")
    print(f"API key: {'configured' if settings.configured else 'not configured'}")
    print(f"Key source: {settings.key_source or 'none'}")
    print(f"Config: {config_path()}")
    return 0
