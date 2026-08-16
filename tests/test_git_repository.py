from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from stress_stack.errors import InputError
from stress_stack.git_repository import GitRepository


def test_extracts_commits_diffs_branches_tags_and_merge(history_repository: Path) -> None:
    repository = GitRepository.discover(history_repository)
    repository.validate()
    records = repository.commit_records()

    assert len(records) == 4
    assert sum(record.is_merge for record in records) == 1
    assert {change.path for record in records for change in record.changed_files} >= {
        "base.txt",
        "feature.txt",
        "main.txt",
    }
    assert any(record.additions > 0 for record in records)
    assert {branch["name"] for branch in repository.branches()} >= {"main", "feature"}
    assert {tag["name"] for tag in repository.tags()} == {"v0.1.0"}


def test_clone_and_reuse_matching_repository(history_repository: Path, tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(history_repository), str(remote)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    destination = tmp_path / "clone"

    first = GitRepository.clone_or_update(str(remote), destination)
    second = GitRepository.clone_or_update(str(remote), destination)

    assert first.action == "cloned"
    assert second.action == "reused"
    assert first.repository.head_sha() == second.repository.head_sha()


def test_refuses_conflicting_existing_destination(
    history_repository: Path, tmp_path: Path
) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(history_repository), str(remote)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    destination = tmp_path / "clone"
    destination.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=destination,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    with pytest.raises(InputError):
        GitRepository.clone_or_update(str(remote), destination)
