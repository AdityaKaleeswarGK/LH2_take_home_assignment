from __future__ import annotations

import argparse
import sys
from pathlib import Path

from inverse_alpha import __version__
from inverse_alpha.errors import InverseAlphaError
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
    return parser


def main(arguments: list[str] | None = None) -> int:
    parser = build_parser()
    namespace = parser.parse_args(arguments)
    try:
        if namespace.command == "ingest":
            result = ingest(namespace.source, cwd=Path.cwd())
        elif namespace.command in {"knowledge", "complete", "completely"}:
            knowledge_result = build_knowledge(namespace.source, cwd=Path.cwd())
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
    print(f"Knowledge: {result.knowledge_root}")
    print(f"Blueprint: {result.knowledge_root / 'blueprint.md'}")
    print(f"Test map: {result.knowledge_root / 'test_map.md'}")
