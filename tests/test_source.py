from pathlib import Path

import pytest

from inverse_alpha.errors import InputError
from inverse_alpha.source import (
    normalize_git_url,
    parse_github_url,
    resolve_source,
    sanitize_git_url,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://github.com/mahmoud/glom", ("mahmoud", "glom")),
        ("https://github.com/mahmoud/glom.git", ("mahmoud", "glom")),
        ("https://github.com/mahmoud/glom/", ("mahmoud", "glom")),
        ("git@github.com:mahmoud/glom.git", ("mahmoud", "glom")),
        ("https://gitlab.com/mahmoud/glom", None),
        ("https://github.com/mahmoud/glom/issues", None),
    ],
)
def test_parse_github_url(value: str, expected: tuple[str, str] | None) -> None:
    assert parse_github_url(value) == expected


def test_resolve_local_path(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    source = resolve_source("repo", tmp_path)
    assert source.kind == "local"
    assert source.local_path == repository.resolve()


def test_resolve_invalid_input(tmp_path: Path) -> None:
    with pytest.raises(InputError):
        resolve_source("not-a-repository", tmp_path)


def test_sanitize_url_removes_embedded_credentials() -> None:
    value = "https://username:secret@github.com/owner/repository.git"
    assert sanitize_git_url(value) == "https://github.com/owner/repository.git"


def test_normalize_github_urls() -> None:
    assert normalize_git_url("git@github.com:Owner/Repository.git") == normalize_git_url(
        "https://github.com/owner/repository"
    )
