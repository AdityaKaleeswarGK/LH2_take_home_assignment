"""Adversarial fixtures for candidate mining.

glom is a flat-layout repository whose test files all begin with ``test_``. It
therefore cannot expose a src-layout module name, a ``.py`` file under ``docs/``,
a binary delta, or a merge commit whose own numstat is empty. Each of those is
built here explicitly, because "passes on the sample repo" is precisely the
failure mode the brief warns about.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stress_stack.candidates import (
    ModuleResolver,
    is_behavioural,
    mine_history,
    module_index,
    numstat,
    percentile,
)
from stress_stack.git_repository import GitRepository
from stress_stack.graph import build_graph

from conftest import run_git


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


@pytest.fixture
def src_layout_repository(tmp_path: Path) -> Path:
    """A src-layout repository with one merged pull request touching source and tests."""
    repository = tmp_path / "srclayout"
    repository.mkdir()
    run_git(repository, "init", "-b", "main")
    run_git(repository, "config", "user.name", "Stress Stack Test")
    run_git(repository, "config", "user.email", "stress-stack@example.test")

    write(repository / "src" / "pkg" / "__init__.py", "from pkg.core import parse\n")
    write(
        repository / "src" / "pkg" / "core.py",
        '"""Core."""\n\n\ndef parse(value):\n    """Parse a value."""\n    return value\n',
    )
    write(repository / "src" / "pkg" / "legacy.py", "OLD = 1\n")
    write(repository / "tests" / "test_core.py", "from pkg.core import parse\n\n\ndef test_parse():\n    assert parse(1) == 1\n")
    write(repository / "docs" / "example.py", "print('docs sample')\n")
    run_git(repository, "add", ".")
    run_git(repository, "commit", "-m", "Initial commit")

    run_git(repository, "checkout", "-b", "feature")
    write(
        repository / "src" / "pkg" / "core.py",
        '"""Core."""\n\n\ndef parse(value):\n    """Parse a value."""\n    if value is None:\n        raise ValueError("value required")\n    return value\n',
    )
    write(
        repository / "tests" / "test_core.py",
        "import pytest\n\nfrom pkg.core import parse\n\n\ndef test_parse():\n    assert parse(1) == 1\n\n\ndef test_parse_rejects_none():\n    with pytest.raises(ValueError):\n        parse(None)\n",
    )
    # A file deleted by the change: absent from HEAD's graph, so it exercises the
    # resolver's fallback rather than its lookup.
    (repository / "src" / "pkg" / "legacy.py").unlink()
    run_git(repository, "add", "-A")
    run_git(repository, "commit", "-m", "Reject None")

    run_git(repository, "checkout", "main")
    run_git(repository, "merge", "--no-ff", "feature", "-m", "Merge pull request #7 from x/feature")
    return repository


def history_for(repository: Path, *, pr_number: int = 7) -> Path:
    """Write the ingest artifacts mining reads, for the repository's merge commit."""
    merge_sha = run_git(repository, "rev-parse", "HEAD")
    history_root = repository / ".stress_stack" / "history"
    write_jsonl(
        history_root / "pull_requests.jsonl",
        [
            {
                "number": pr_number,
                "title": "Reject None",
                "body": "Raise ValueError when the value is missing.",
                "state": "closed",
                "merged_at": "2024-01-01T00:00:00Z",
                "merge_commit_sha": merge_sha,
                "html_url": f"https://example.test/pull/{pr_number}",
                "author": "someone",
            }
        ],
    )
    write_jsonl(history_root / "commits.jsonl", [{"sha": merge_sha}])
    write_jsonl(
        history_root / "commit_pr_links.jsonl",
        [{"commit_sha": merge_sha, "pr_number": pr_number, "method": "github_merge_sha"}],
    )
    return history_root


def test_percentile_handles_degenerate_inputs() -> None:
    assert percentile([], 0.9) == 0
    assert percentile([42], 0.9) == 42
    assert percentile([1, 2], 0.0) == 1
    assert percentile([1, 2], 1.0) == 2
    assert percentile([1, 2, 3, 4, 5], 0.5) == 3


def test_docs_python_file_is_not_behavioural() -> None:
    assert not is_behavioural("docs/example.py")
    assert not is_behavioural("nested/docs/example.py")
    assert not is_behavioural("README.md")
    assert not is_behavioural(".github/workflows/ci.yml")
    assert is_behavioural("glom/core.py")
    assert is_behavioural("tests/test_core.py")


def test_module_resolver_strips_the_layout_prefix_it_measured(src_layout_repository: Path) -> None:
    resolver = module_index(build_graph(src_layout_repository))

    # A path the graph knows resolves from the verified parse.
    assert resolver.of("src/pkg/core.py") == "pkg.core"
    # A path it does not know must be named the same way, not carry `src.`.
    assert resolver.of("src/pkg/legacy.py") == "pkg.legacy"
    assert resolver.of("src/pkg/sub/other.py") == "pkg.sub.other"
    # A non-Python path has no module and must stay distinguishable.
    assert resolver.of("Makefile") == "Makefile"


def test_module_resolver_without_a_prefix_is_flat() -> None:
    resolver = ModuleResolver(known={"glom/core.py": "glom.core"}, prefixes=("",))
    assert resolver.of("glom/mutable.py") == "glom.mutable"
    assert resolver.of("glom/__init__.py") == "glom"


def test_numstat_reads_a_merge_commit_between_trees(src_layout_repository: Path) -> None:
    """A merge commit's own numstat is empty; the change lives between trees."""
    repository = GitRepository.discover(src_layout_repository)
    head = repository.run(["rev-parse", "HEAD"], record=False).strip()
    base = repository.run(["rev-list", "--parents", "-n", "1", head], record=False).split()[1]

    deltas = {delta.path: delta for delta in numstat(repository, base, head)}
    assert "src/pkg/core.py" in deltas
    assert "tests/test_core.py" in deltas
    assert deltas["src/pkg/legacy.py"].deletions == 1
    assert all(not delta.binary for delta in deltas.values())


def test_binary_delta_does_not_break_parsing(src_layout_repository: Path) -> None:
    write(src_layout_repository / "asset.bin", "")
    (src_layout_repository / "asset.bin").write_bytes(bytes(range(256)))
    run_git(src_layout_repository, "add", "asset.bin")
    run_git(src_layout_repository, "commit", "-m", "Add binary asset")

    repository = GitRepository.discover(src_layout_repository)
    deltas = {d.path: d for d in numstat(repository, "HEAD~1", "HEAD")}
    assert deltas["asset.bin"].binary is True
    assert deltas["asset.bin"].churn == 0


def test_mining_keeps_a_pull_request_that_changed_source_and_tests(
    src_layout_repository: Path,
) -> None:
    history_root = history_for(src_layout_repository)
    repository = GitRepository.discover(src_layout_repository)
    graph = build_graph(src_layout_repository)

    candidates, funnel, thresholds = mine_history(repository, graph, history_root)

    assert funnel.considered == 1
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.candidate_id == "pr-7"
    assert candidate.primary_module == "pkg.core"
    # The deleted file must appear as its own module, named consistently.
    assert "pkg.legacy" in candidate.modules
    assert candidate.signals["test_paths"] == ["tests/test_core.py"]
    # docs/example.py was untouched here; the source set must hold only real source.
    assert candidate.signals["source_paths"] == ["src/pkg/core.py", "src/pkg/legacy.py"]
    assert thresholds["churn_ceiling"] >= candidate.signals["churn"]


def test_documentation_only_change_is_dropped_with_a_reason(
    src_layout_repository: Path,
) -> None:
    write(src_layout_repository / "docs" / "example.py", "print('changed')\n")
    write(src_layout_repository / "README.md", "# changed\n")
    run_git(src_layout_repository, "add", "-A")
    run_git(src_layout_repository, "commit", "-m", "Docs only")
    history_root = history_for(src_layout_repository, pr_number=8)

    repository = GitRepository.discover(src_layout_repository)
    candidates, funnel, _ = mine_history(repository, build_graph(src_layout_repository), history_root)

    assert candidates == []
    assert funnel.dropped["no_source_change"] == ["PR#8"]


def test_unmerged_pull_requests_are_never_considered(src_layout_repository: Path) -> None:
    history_root = history_for(src_layout_repository)
    records = [
        json.loads(line)
        for line in (history_root / "pull_requests.jsonl").read_text().splitlines()
    ]
    records[0]["merged_at"] = None
    write_jsonl(history_root / "pull_requests.jsonl", records)

    repository = GitRepository.discover(src_layout_repository)
    candidates, funnel, _ = mine_history(repository, build_graph(src_layout_repository), history_root)

    assert candidates == []
    assert funnel.considered == 0
