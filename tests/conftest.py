from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


def run_git(repository: Path, *arguments: str) -> str:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_NAME": "Stress Stack Test",
            "GIT_AUTHOR_EMAIL": "stress-stack@example.test",
            "GIT_COMMITTER_NAME": "Stress Stack Test",
            "GIT_COMMITTER_EMAIL": "stress-stack@example.test",
            "LC_ALL": "C",
        }
    )
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        env=environment,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def history_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "history-repository"
    repository.mkdir()
    run_git(repository, "init", "-b", "main")
    run_git(repository, "config", "user.name", "Stress Stack Test")
    run_git(repository, "config", "user.email", "stress-stack@example.test")

    (repository / "base.txt").write_text("base\n", encoding="utf-8")
    run_git(repository, "add", "base.txt")
    run_git(repository, "commit", "-m", "Initial commit")

    run_git(repository, "checkout", "-b", "feature")
    (repository / "feature.txt").write_text("feature\n", encoding="utf-8")
    run_git(repository, "add", "feature.txt")
    run_git(repository, "commit", "-m", "Add feature")

    run_git(repository, "checkout", "main")
    (repository / "main.txt").write_text("main\n", encoding="utf-8")
    run_git(repository, "add", "main.txt")
    run_git(repository, "commit", "-m", "Update main")
    run_git(
        repository,
        "merge",
        "--no-ff",
        "feature",
        "-m",
        "Merge pull request #7 from example/feature",
    )
    run_git(repository, "tag", "v0.1.0")
    return repository


@pytest.fixture(autouse=True)
def _isolate_the_known_bad_ledger(tmp_path_factory, monkeypatch):
    """Keep the cross-run image ledger out of the developer's home directory.

    `runtime_matrix` persists base images that failed to build so tomorrow's run
    does not pay to rediscover them. It is global mutable state, and without
    this the suite wrote fake failures into a real `~/.stress_stack` — which a
    later test then read back, so whether it passed depended on what had run
    before it.
    """
    monkeypatch.setenv(
        "STRESS_STACK_HOME", str(tmp_path_factory.mktemp("stress-stack-home"))
    )
