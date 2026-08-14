from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from inverse_alpha import __version__
from inverse_alpha.atomic import append_jsonl, atomic_write_json, atomic_write_jsonl
from inverse_alpha.git_repository import GitRepository
from inverse_alpha.github import GitHubClient, PullRequestFetchResult
from inverse_alpha.models import (
    AvailabilityEntry,
    CommitPrLink,
    CommitRecord,
    PullRequestRecord,
)
from inverse_alpha.source import SourceSpec, resolve_source, sanitize_git_url

_MERGE_PR_PATTERN = re.compile(r"Merge pull request #(\d+)")
_SQUASH_PR_PATTERN = re.compile(r"\(#(\d+)\)\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class IngestionResult:
    repository_root: Path
    metadata_root: Path
    head_sha: str
    commit_count: int
    pull_request_count: int
    pull_request_status: str
    action: str


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def ingest(source_value: str, *, cwd: Path | None = None) -> IngestionResult:
    working_directory = (cwd or Path.cwd()).resolve()
    source = resolve_source(source_value, working_directory)
    started_at = utc_now()
    start_clock = time.monotonic()

    repository, action = _prepare_repository(source, working_directory)
    repository.validate()
    repository.ensure_full_history()
    dirty_before, dirty_entries = repository.dirty_status()
    repository.add_metadata_exclude()

    metadata_root = repository.root / ".inverse_alpha"
    history_root = metadata_root / "history"
    log_path = metadata_root / "logs" / "ingestion.jsonl"
    state_root = metadata_root / "state"
    history_root.mkdir(parents=True, exist_ok=True)
    state_root.mkdir(parents=True, exist_ok=True)

    try:
        commits = repository.commit_records()
        coordinates = source_coordinates(source, repository)
        pull_result = _fetch_pull_requests(coordinates)
        links = link_pull_requests(commits, pull_result.records)

        head_sha = repository.head_sha()
        branches = repository.branches()
        tags = repository.tags()
        contributor_count = len(
            {(commit.author_name, commit.author_email) for commit in commits}
        )
        merge_count = sum(1 for commit in commits if commit.is_merge)
        completed_at = utc_now()
        origin = sanitize_git_url(repository.origin_url())
        owner = coordinates[0] if coordinates is not None else None
        repository_name = coordinates[1] if coordinates is not None else repository.root.name

        manifest = _manifest(metadata_root, started_at, completed_at)
        repository_metadata: dict[str, Any] = {
            "schema_version": "0.1.0",
            "input": {
                "kind": source.kind,
                "value": source.clone_url if source.kind == "github" else str(repository.root),
            },
            "identity": {
                "owner": owner,
                "repository": repository_name,
                "origin": origin,
            },
            "repository_root": str(repository.root),
            "default_branch": repository.default_branch(),
            "current_branch": repository.current_branch(),
            "head_sha": head_sha,
            "is_shallow": repository.is_shallow(),
            "dirty_before_ingestion": dirty_before,
            "dirty_entries_before_ingestion": dirty_entries,
            "branches": branches,
            "tags": tags,
            "captured_at": completed_at,
        }
        availability = {
            "schema_version": "0.1.0",
            "generated_at": completed_at,
            "capabilities": {
                "commits": AvailabilityEntry("available", len(commits)).to_dict(),
                "commit_diffs": AvailabilityEntry("available", len(commits)).to_dict(),
                "branches": AvailabilityEntry("available", len(branches)).to_dict(),
                "tags": AvailabilityEntry("available", len(tags)).to_dict(),
                "merge_commits": AvailabilityEntry("available", merge_count).to_dict(),
                "contributors": AvailabilityEntry("available", contributor_count).to_dict(),
                "pull_requests": AvailabilityEntry(
                    pull_result.status,
                    len(pull_result.records),
                    pull_result.reason,
                ).to_dict(),
                "pull_request_reviews": AvailabilityEntry(
                    "not_collected", None, "outside_milestone_1"
                ).to_dict(),
                "pull_request_comments": AvailabilityEntry(
                    "not_collected", None, "outside_milestone_1"
                ).to_dict(),
                "issues": AvailabilityEntry(
                    "not_collected", None, "outside_milestone_1"
                ).to_dict(),
                "releases": AvailabilityEntry(
                    "not_collected", None, "outside_milestone_1"
                ).to_dict(),
                "ci_runs": AvailabilityEntry(
                    "not_collected", None, "outside_milestone_1"
                ).to_dict(),
            },
        }
        _preserve_downstream_capabilities(metadata_root, availability)
        latest = {
            "schema_version": "0.1.0",
            "status": "complete" if pull_result.status == "available" else "partial",
            "head_sha": head_sha,
            "generated_at": completed_at,
            "files": [
                "manifest.json",
                "repository.json",
                "availability.json",
                "history/commits.jsonl",
                "history/pull_requests.jsonl",
                "history/commit_pr_links.jsonl",
            ],
        }

        atomic_write_json(metadata_root / "manifest.json", manifest)
        atomic_write_json(metadata_root / "repository.json", repository_metadata)
        atomic_write_json(metadata_root / "availability.json", availability)
        atomic_write_jsonl(history_root / "commits.jsonl", (item.to_dict() for item in commits))
        atomic_write_jsonl(
            history_root / "pull_requests.jsonl",
            (item.to_dict() for item in pull_result.records),
        )
        atomic_write_jsonl(
            history_root / "commit_pr_links.jsonl",
            (item.to_dict() for item in links),
        )
        atomic_write_json(state_root / "latest.json", latest)

        duration = round(time.monotonic() - start_clock, 3)
        append_jsonl(
            log_path,
            {
                "event": "ingestion",
                "status": latest["status"],
                "started_at": started_at,
                "completed_at": completed_at,
                "duration_seconds": duration,
                "source_kind": source.kind,
                "repository_root": str(repository.root),
                "head_sha": head_sha,
                "action": action,
                "commands": repository.command_log,
                "counts": {
                    "commits": len(commits),
                    "pull_requests": len(pull_result.records),
                    "commit_pr_links": len(links),
                },
                "warnings": [pull_result.reason] if pull_result.reason else [],
                "github_rate_limit": {
                    "remaining": pull_result.rate_limit_remaining,
                    "reset": pull_result.rate_limit_reset,
                },
            },
        )
        return IngestionResult(
            repository_root=repository.root,
            metadata_root=metadata_root,
            head_sha=head_sha,
            commit_count=len(commits),
            pull_request_count=len(pull_result.records),
            pull_request_status=pull_result.status,
            action=action,
        )
    except Exception as exc:
        append_jsonl(
            log_path,
            {
                "event": "ingestion",
                "status": "failed",
                "started_at": started_at,
                "completed_at": utc_now(),
                "duration_seconds": round(time.monotonic() - start_clock, 3),
                "source_kind": source.kind,
                "repository_root": str(repository.root),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "commands": repository.command_log,
            },
        )
        raise


def _prepare_repository(source: SourceSpec, cwd: Path) -> tuple[GitRepository, str]:
    if source.kind == "local":
        assert source.local_path is not None
        return GitRepository.discover(source.local_path), "analyzed_local"
    assert source.repository is not None
    assert source.clone_url is not None
    destination = cwd / source.repository
    prepared = GitRepository.clone_or_update(source.clone_url, destination)
    return prepared.repository, prepared.action


def source_coordinates(
    source: SourceSpec, repository: GitRepository
) -> tuple[str, str] | None:
    if source.owner is not None and source.repository is not None:
        return source.owner, source.repository
    return repository.github_coordinates()


def _fetch_pull_requests(
    coordinates: tuple[str, str] | None,
) -> PullRequestFetchResult:
    if coordinates is None:
        return PullRequestFetchResult(
            records=[],
            status="unavailable",
            reason="no_public_github_origin",
            rate_limit_remaining=None,
            rate_limit_reset=None,
        )
    owner, repository = coordinates
    return GitHubClient(owner, repository).list_pull_requests()


def link_pull_requests(
    commits: list[CommitRecord], pull_requests: list[PullRequestRecord]
) -> list[CommitPrLink]:
    commit_shas = {commit.sha for commit in commits}
    pull_request_numbers = {pull_request.number for pull_request in pull_requests}
    links: dict[tuple[str, int], CommitPrLink] = {}

    for pull_request in pull_requests:
        merge_sha = pull_request.merge_commit_sha
        if pull_request.merged_at and merge_sha and merge_sha in commit_shas:
            link = CommitPrLink(merge_sha, pull_request.number, "github_merge_sha")
            links[(link.commit_sha, link.pr_number)] = link

    for commit in commits:
        message = f"{commit.subject}\n{commit.body}"
        numbers = {
            int(match)
            for pattern in (_MERGE_PR_PATTERN, _SQUASH_PR_PATTERN)
            for match in pattern.findall(message)
        }
        for number in numbers & pull_request_numbers:
            key = (commit.sha, number)
            if key not in links:
                links[key] = CommitPrLink(commit.sha, number, "commit_message")

    return sorted(links.values(), key=lambda item: (item.pr_number, item.commit_sha))


def _manifest(metadata_root: Path, started_at: str, completed_at: str) -> dict[str, Any]:
    created_at = started_at
    stages: dict[str, str] = {
        "history_ingestion": "complete",
        "repo_hygiene": "not_started",
        "knowledge_layer": "not_started",
        "task_generation": "not_started",
    }
    path = metadata_root / "manifest.json"
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(previous, dict) and isinstance(previous.get("created_at"), str):
                created_at = previous["created_at"]
            previous_stages = previous.get("stages") if isinstance(previous, dict) else None
            if isinstance(previous_stages, dict):
                for name in stages:
                    if isinstance(previous_stages.get(name), str):
                        stages[name] = previous_stages[name]
        except (OSError, json.JSONDecodeError):
            pass
    stages["history_ingestion"] = "complete"
    return {
        "schema_version": "0.1.0",
        "tool": {"name": "inverse-alpha", "version": __version__},
        "created_at": created_at,
        "updated_at": completed_at,
        "stages": stages,
    }


def _preserve_downstream_capabilities(
    metadata_root: Path, availability: dict[str, Any]
) -> None:
    path = metadata_root / "availability.json"
    if not path.exists():
        return
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    previous_capabilities = previous.get("capabilities") if isinstance(previous, dict) else None
    capabilities = availability["capabilities"]
    if not isinstance(previous_capabilities, dict):
        return
    for name, value in previous_capabilities.items():
        if name not in capabilities and isinstance(value, dict):
            capabilities[name] = value
