from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from stress_stack.errors import InputError

_GITHUB_SSH_PATTERN = re.compile(
    r"^git@github\.com:(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?$"
)


@dataclass(frozen=True, slots=True)
class SourceSpec:
    kind: str
    raw: str
    local_path: Path | None = None
    owner: str | None = None
    repository: str | None = None
    clone_url: str | None = None


def parse_github_url(value: str) -> tuple[str, str] | None:
    ssh_match = _GITHUB_SSH_PATTERN.fullmatch(value.strip())
    if ssh_match:
        return ssh_match.group("owner"), ssh_match.group("repo")

    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or parsed.hostname not in {"github.com", "www.github.com"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        return None
    owner, repository = parts
    if repository.endswith(".git"):
        repository = repository[:-4]
    valid_component = re.compile(r"^[A-Za-z0-9_.-]+$")
    if not owner or not repository:
        return None
    if not valid_component.fullmatch(owner) or not valid_component.fullmatch(repository):
        return None
    return owner, repository


def resolve_source(value: str, cwd: Path) -> SourceSpec:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    if candidate.exists():
        return SourceSpec(kind="local", raw=value, local_path=candidate.resolve())

    coordinates = parse_github_url(value)
    if coordinates is None:
        raise InputError(
            "Input must be an existing local Git repository or a public "
            "https://github.com/<owner>/<repository> URL"
        )
    owner, repository = coordinates
    clone_url = f"https://github.com/{owner}/{repository}.git"
    return SourceSpec(
        kind="github",
        raw=value,
        owner=owner,
        repository=repository,
        clone_url=clone_url,
    )


def sanitize_git_url(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if _GITHUB_SSH_PATTERN.fullmatch(stripped):
        return stripped
    parsed = urlparse(stripped)
    if not parsed.scheme or not parsed.netloc:
        return stripped
    hostname = parsed.hostname or ""
    if parsed.port is not None:
        hostname = f"{hostname}:{parsed.port}"
    sanitized = parsed._replace(netloc=hostname)
    return urlunparse(sanitized)


def normalize_git_url(value: str) -> str:
    coordinates = parse_github_url(value)
    if coordinates is not None:
        owner, repository = coordinates
        return f"github.com/{owner.lower()}/{repository.lower()}"
    sanitized = sanitize_git_url(value) or value
    return sanitized.removesuffix(".git").rstrip("/")
