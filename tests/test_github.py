from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlparse

from inverse_alpha.github import GitHubClient, RequestResult


def pull_request(number: int) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Pull request {number}",
        "body": "Description",
        "state": "closed",
        "draft": False,
        "user": {"login": "contributor"},
        "labels": [{"name": "feature"}],
        "base": {"ref": "main"},
        "head": {"ref": f"feature-{number}"},
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "closed_at": "2026-01-02T00:00:00Z",
        "merged_at": "2026-01-02T00:00:00Z",
        "merge_commit_sha": f"{number:040x}",
        "html_url": f"https://github.com/example/repository/pull/{number}",
    }


def test_lists_paginated_pull_requests_with_optional_token() -> None:
    requests: list[tuple[str, Mapping[str, str]]] = []

    def requester(url: str, headers: Mapping[str, str]) -> RequestResult:
        requests.append((url, headers))
        page = int(parse_qs(urlparse(url).query)["page"][0])
        if page == 1:
            payload = [pull_request(number) for number in range(1, 101)]
        else:
            payload = [pull_request(101)]
        return 200, {"X-RateLimit-Remaining": "4998", "X-RateLimit-Reset": "42"}, payload

    result = GitHubClient(
        "example", "repository", token="test-token", requester=requester
    ).list_pull_requests()

    assert result.status == "available"
    assert result.reason is None
    assert len(result.records) == 101
    assert len(requests) == 2
    assert requests[0][1]["Authorization"] == "Bearer test-token"
    assert result.rate_limit_remaining == 4998
    assert result.records[0].labels == ["feature"]


def test_rate_limit_after_first_page_is_partial() -> None:
    call_count = 0

    def requester(url: str, headers: Mapping[str, str]) -> RequestResult:
        del url, headers
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return 200, {"X-RateLimit-Remaining": "0"}, [
                pull_request(number) for number in range(1, 101)
            ]
        return 403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "42"}, {
            "message": "API rate limit exceeded"
        }

    result = GitHubClient("example", "repository", requester=requester).list_pull_requests()

    assert result.status == "partial"
    assert len(result.records) == 100
    assert result.reason == "github_http_403: API rate limit exceeded"


def test_first_page_failure_is_unavailable() -> None:
    def requester(url: str, headers: Mapping[str, str]) -> RequestResult:
        del url, headers
        return 0, {}, {"message": "network unavailable"}

    result = GitHubClient("example", "repository", requester=requester).list_pull_requests()
    assert result.status == "unavailable"
    assert result.records == []
    assert result.reason == "github_http_0: network unavailable"
