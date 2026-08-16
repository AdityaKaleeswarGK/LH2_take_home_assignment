from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from stress_stack.errors import MetadataError

AvailabilityStatus = Literal["available", "partial", "unavailable", "not_collected"]


@dataclass(frozen=True, slots=True)
class FileChange:
    path: str
    additions: int | None
    deletions: int | None
    binary: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CommitRecord:
    sha: str
    parent_shas: list[str]
    author_name: str
    author_email: str
    authored_at: str
    committer_name: str
    committer_email: str
    committed_at: str
    refs: list[str]
    subject: str
    body: str
    is_merge: bool
    changed_files: list[FileChange]
    additions: int
    deletions: int

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        validate_commit(value)
        return value


@dataclass(frozen=True, slots=True)
class PullRequestRecord:
    number: int
    title: str
    body: str
    state: str
    draft: bool
    author: str | None
    labels: list[str]
    base_ref: str
    head_ref: str
    created_at: str | None
    updated_at: str | None
    closed_at: str | None
    merged_at: str | None
    merge_commit_sha: str | None
    html_url: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        validate_pull_request(value)
        return value


@dataclass(frozen=True, slots=True)
class CommitPrLink:
    commit_sha: str
    pr_number: int
    method: Literal["github_merge_sha", "commit_message"]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        validate_commit_pr_link(value)
        return value


@dataclass(frozen=True, slots=True)
class AvailabilityEntry:
    status: AvailabilityStatus
    count: int | None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _require(record: dict[str, Any], name: str, expected: type | tuple[type, ...]) -> None:
    if name not in record:
        raise MetadataError(f"Generated record is missing required field {name!r}")
    if not isinstance(record[name], expected):
        raise MetadataError(
            f"Generated field {name!r} has type {type(record[name]).__name__}, "
            f"expected {expected}"
        )


def validate_commit(record: dict[str, Any]) -> None:
    for field_name in (
        "sha",
        "author_name",
        "author_email",
        "authored_at",
        "committer_name",
        "committer_email",
        "committed_at",
        "subject",
        "body",
    ):
        _require(record, field_name, str)
    _require(record, "parent_shas", list)
    _require(record, "refs", list)
    _require(record, "is_merge", bool)
    _require(record, "changed_files", list)
    _require(record, "additions", int)
    _require(record, "deletions", int)


def validate_pull_request(record: dict[str, Any]) -> None:
    _require(record, "number", int)
    for field_name in ("title", "body", "state", "base_ref", "head_ref", "html_url"):
        _require(record, field_name, str)
    _require(record, "draft", bool)
    _require(record, "labels", list)


def validate_commit_pr_link(record: dict[str, Any]) -> None:
    _require(record, "commit_sha", str)
    _require(record, "pr_number", int)
    _require(record, "method", str)
