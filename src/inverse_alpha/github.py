from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from inverse_alpha.models import AvailabilityStatus, PullRequestRecord

RequestResult = tuple[int, Mapping[str, str], Any]
Requester = Callable[[str, Mapping[str, str]], RequestResult]


@dataclass(frozen=True, slots=True)
class PullRequestFetchResult:
    records: list[PullRequestRecord]
    status: AvailabilityStatus
    reason: str | None
    rate_limit_remaining: int | None
    rate_limit_reset: int | None


class GitHubClient:
    def __init__(
        self,
        owner: str,
        repository: str,
        *,
        token: str | None = None,
        requester: Requester | None = None,
    ) -> None:
        self.owner = owner
        self.repository = repository
        self._token = token if token is not None else os.environ.get("GITHUB_TOKEN")
        self._requester = requester or self._request_json

    def list_pull_requests(self) -> PullRequestFetchResult:
        records: list[PullRequestRecord] = []
        page = 1
        rate_remaining: int | None = None
        rate_reset: int | None = None
        while True:
            query = urlencode(
                {
                    "state": "all",
                    "per_page": 100,
                    "page": page,
                    "sort": "created",
                    "direction": "asc",
                }
            )
            url = (
                f"https://api.github.com/repos/{self.owner}/{self.repository}/pulls?{query}"
            )
            status_code, headers, payload = self._requester(url, self._headers())
            normalized_headers = {key.lower(): value for key, value in headers.items()}
            rate_remaining = _integer_or_none(normalized_headers.get("x-ratelimit-remaining"))
            rate_reset = _integer_or_none(normalized_headers.get("x-ratelimit-reset"))
            if status_code != 200:
                status: AvailabilityStatus = "partial" if records else "unavailable"
                message = _payload_message(payload)
                reason = f"github_http_{status_code}"
                if message:
                    reason = f"{reason}: {message}"
                return PullRequestFetchResult(
                    records=records,
                    status=status,
                    reason=reason,
                    rate_limit_remaining=rate_remaining,
                    rate_limit_reset=rate_reset,
                )
            if not isinstance(payload, list):
                return PullRequestFetchResult(
                    records=records,
                    status="partial" if records else "unavailable",
                    reason="github_invalid_response",
                    rate_limit_remaining=rate_remaining,
                    rate_limit_reset=rate_reset,
                )
            records.extend(
                _pull_request_from_api(item) for item in payload if isinstance(item, dict)
            )
            if len(payload) < 100:
                return PullRequestFetchResult(
                    records=records,
                    status="available",
                    reason=None,
                    rate_limit_remaining=rate_remaining,
                    rate_limit_reset=rate_reset,
                )
            page += 1

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "inverse-alpha/0.1.0",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    @staticmethod
    def _request_json(url: str, headers: Mapping[str, str]) -> RequestResult:
        request = Request(url=url, headers=dict(headers), method="GET")
        try:
            with urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
                return response.status, dict(response.headers.items()), payload
        except HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = {"message": exc.reason}
            return exc.code, dict(exc.headers.items()), payload
        except URLError as exc:
            return 0, {}, {"message": str(exc.reason)}
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return 0, {}, {"message": f"Invalid JSON response: {exc}"}


def _pull_request_from_api(payload: dict[str, Any]) -> PullRequestRecord:
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    base = payload.get("base") if isinstance(payload.get("base"), dict) else {}
    head = payload.get("head") if isinstance(payload.get("head"), dict) else {}
    labels_payload = payload.get("labels") if isinstance(payload.get("labels"), list) else []
    labels = [
        str(label.get("name"))
        for label in labels_payload
        if isinstance(label, dict) and label.get("name") is not None
    ]
    return PullRequestRecord(
        number=int(payload.get("number", 0)),
        title=str(payload.get("title") or ""),
        body=str(payload.get("body") or ""),
        state=str(payload.get("state") or ""),
        draft=bool(payload.get("draft", False)),
        author=str(user.get("login")) if user.get("login") is not None else None,
        labels=labels,
        base_ref=str(base.get("ref") or ""),
        head_ref=str(head.get("ref") or ""),
        created_at=_optional_string(payload.get("created_at")),
        updated_at=_optional_string(payload.get("updated_at")),
        closed_at=_optional_string(payload.get("closed_at")),
        merged_at=_optional_string(payload.get("merged_at")),
        merge_commit_sha=_optional_string(payload.get("merge_commit_sha")),
        html_url=str(payload.get("html_url") or ""),
    )


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None else None


def _integer_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _payload_message(payload: Any) -> str | None:
    if isinstance(payload, dict) and payload.get("message") is not None:
        return str(payload["message"])
    return None
