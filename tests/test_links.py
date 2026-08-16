from stress_stack.ingest import link_pull_requests
from stress_stack.models import CommitRecord, PullRequestRecord


def commit(sha: str, subject: str) -> CommitRecord:
    return CommitRecord(
        sha=sha,
        parent_shas=["parent"],
        author_name="Author",
        author_email="author@example.test",
        authored_at="2026-01-01T00:00:00Z",
        committer_name="Committer",
        committer_email="committer@example.test",
        committed_at="2026-01-01T00:00:00Z",
        refs=[],
        subject=subject,
        body="",
        is_merge=False,
        changed_files=[],
        additions=0,
        deletions=0,
    )


def pull_request(number: int, merge_sha: str | None) -> PullRequestRecord:
    return PullRequestRecord(
        number=number,
        title="Title",
        body="",
        state="closed",
        draft=False,
        author="author",
        labels=[],
        base_ref="main",
        head_ref="feature",
        created_at=None,
        updated_at=None,
        closed_at=None,
        merged_at="2026-01-01T00:00:00Z",
        merge_commit_sha=merge_sha,
        html_url=f"https://github.com/example/repository/pull/{number}",
    )


def test_links_exact_merge_sha_and_squash_message() -> None:
    commits = [
        commit("a" * 40, "Merged feature"),
        commit("b" * 40, "Add another feature (#8)"),
    ]
    pull_requests = [pull_request(7, "a" * 40), pull_request(8, "c" * 40)]

    links = link_pull_requests(commits, pull_requests)

    assert [(link.pr_number, link.commit_sha, link.method) for link in links] == [
        (7, "a" * 40, "github_merge_sha"),
        (8, "b" * 40, "commit_message"),
    ]
