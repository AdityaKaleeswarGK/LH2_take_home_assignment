from __future__ import annotations

import json
import subprocess
from pathlib import Path

from inverse_alpha.ingest import ingest


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_local_ingestion_creates_repository_metadata(history_repository: Path) -> None:
    result = ingest(str(history_repository), cwd=history_repository.parent)
    metadata = history_repository / ".inverse_alpha"

    assert result.repository_root == history_repository
    assert result.commit_count == 4
    assert result.pull_request_status == "unavailable"
    assert result.metadata_root == metadata

    expected_files = {
        metadata / "manifest.json",
        metadata / "repository.json",
        metadata / "availability.json",
        metadata / "history" / "commits.jsonl",
        metadata / "history" / "pull_requests.jsonl",
        metadata / "history" / "commit_pr_links.jsonl",
        metadata / "logs" / "ingestion.jsonl",
        metadata / "state" / "latest.json",
    }
    assert all(path.is_file() for path in expected_files)

    commits = read_jsonl(metadata / "history" / "commits.jsonl")
    availability = json.loads((metadata / "availability.json").read_text(encoding="utf-8"))
    repository = json.loads((metadata / "repository.json").read_text(encoding="utf-8"))
    latest = json.loads((metadata / "state" / "latest.json").read_text(encoding="utf-8"))

    assert len(commits) == 4
    assert all("sha" in commit and "changed_files" in commit for commit in commits)
    assert availability["capabilities"]["commits"]["status"] == "available"
    assert availability["capabilities"]["pull_requests"]["status"] == "unavailable"
    assert availability["capabilities"]["issues"]["status"] == "not_collected"
    assert repository["dirty_before_ingestion"] is False
    assert latest["head_sha"] == result.head_sha
    assert latest["status"] == "partial"

    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=history_repository,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout
    assert status == ""


def test_repeated_ingestion_replaces_history_without_duplicates(
    history_repository: Path,
) -> None:
    ingest(str(history_repository), cwd=history_repository.parent)
    ingest(str(history_repository), cwd=history_repository.parent)

    metadata = history_repository / ".inverse_alpha"
    commits = read_jsonl(metadata / "history" / "commits.jsonl")
    logs = read_jsonl(metadata / "logs" / "ingestion.jsonl")

    assert len(commits) == 4
    assert len({commit["sha"] for commit in commits}) == 4
    assert len(logs) == 2
    assert all(log["status"] == "partial" for log in logs)

