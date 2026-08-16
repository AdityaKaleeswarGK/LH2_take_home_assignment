from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from stress_stack.errors import GitError, InputError
from stress_stack.models import CommitRecord, FileChange
from stress_stack.source import normalize_git_url, parse_github_url, sanitize_git_url


def _command_text(arguments: list[str]) -> str:
    return "git " + " ".join(arguments)


def _run_process(
    arguments: list[str], cwd: Path | None = None
) -> subprocess.CompletedProcess[bytes]:
    environment = os.environ.copy()
    environment.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    try:
        return subprocess.run(
            arguments,
            cwd=cwd,
            env=environment,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise GitError(f"Could not execute {arguments[0]}: {exc}") from exc


@dataclass(slots=True)
class PreparedRepository:
    repository: "GitRepository"
    action: str


class GitRepository:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.command_log: list[str] = []

    @classmethod
    def discover(cls, path: Path) -> "GitRepository":
        result = _run_process(["git", "-C", str(path), "rev-parse", "--show-toplevel"])
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise InputError(f"Local input is not a Git repository: {path}. {detail}")
        root = Path(result.stdout.decode("utf-8", errors="replace").strip())
        return cls(root)

    @classmethod
    def clone_or_update(cls, url: str, destination: Path) -> PreparedRepository:
        sanitized_url = sanitize_git_url(url) or url
        if not destination.exists():
            result = _run_process(
                ["git", "clone", "--no-single-branch", "--origin", "origin", url, str(destination)]
            )
            if result.returncode != 0:
                detail = result.stderr.decode("utf-8", errors="replace").strip()
                raise GitError(f"Could not clone {sanitized_url}: {detail}")
            repository = cls.discover(destination)
            repository.command_log.append(
                f"git clone --no-single-branch --origin origin {sanitized_url} {destination}"
            )
            return PreparedRepository(repository=repository, action="cloned")

        repository = cls.discover(destination)
        origin = repository.origin_url()
        if origin is None or normalize_git_url(origin) != normalize_git_url(url):
            raise InputError(
                f"Destination already exists but is not a clone of {sanitized_url}: {destination}"
            )
        repository.validate()
        repository.run(["fetch", "--all", "--tags", "--prune"])
        repository.ensure_full_history()
        return PreparedRepository(repository=repository, action="reused")

    def run(self, arguments: list[str], *, record: bool = True) -> str:
        result = _run_process(["git", *arguments], cwd=self.root)
        if record:
            self.command_log.append(_command_text(arguments))
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise GitError(f"{_command_text(arguments)} failed in {self.root}: {detail}")
        return result.stdout.decode("utf-8", errors="replace")

    def run_bytes(self, arguments: list[str], *, record: bool = True) -> bytes:
        result = _run_process(["git", *arguments], cwd=self.root)
        if record:
            self.command_log.append(_command_text(arguments))
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise GitError(f"{_command_text(arguments)} failed in {self.root}: {detail}")
        return result.stdout

    def validate(self) -> None:
        self.run(["rev-parse", "--is-inside-work-tree"])
        self.run(["fsck", "--connectivity-only"])

    def is_shallow(self) -> bool:
        return self.run(["rev-parse", "--is-shallow-repository"]).strip() == "true"

    def ensure_full_history(self) -> None:
        if not self.is_shallow():
            return
        if self.origin_url() is None:
            raise GitError(
                "Repository is shallow and has no origin from which to retrieve full history"
            )
        self.run(["fetch", "--unshallow", "--tags"])

    def origin_url(self) -> str | None:
        result = _run_process(["git", "remote", "get-url", "origin"], cwd=self.root)
        if result.returncode != 0:
            return None
        return result.stdout.decode("utf-8", errors="replace").strip()

    def github_coordinates(self) -> tuple[str, str] | None:
        origin = self.origin_url()
        if origin is None:
            return None
        return parse_github_url(origin)

    def head_sha(self) -> str:
        return self.run(["rev-parse", "HEAD"]).strip()

    def current_branch(self) -> str | None:
        result = _run_process(["git", "symbolic-ref", "--quiet", "--short", "HEAD"], cwd=self.root)
        if result.returncode != 0:
            return None
        return result.stdout.decode("utf-8", errors="replace").strip()

    def default_branch(self) -> str | None:
        result = _run_process(
            ["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
            cwd=self.root,
        )
        if result.returncode == 0:
            value = result.stdout.decode("utf-8", errors="replace").strip()
            return value.removeprefix("origin/")
        return self.current_branch()

    def dirty_status(self) -> tuple[bool, list[str]]:
        lines = [
            line
            for line in self.run(["status", "--porcelain", "--untracked-files=all"]).splitlines()
            if line.strip()
        ]
        return bool(lines), lines

    def branches(self) -> list[dict[str, str]]:
        output = self.run(
            [
                "for-each-ref",
                "--format=%(refname:short)%00%(objectname)",
                "refs/heads",
                "refs/remotes",
            ]
        )
        values: list[dict[str, str]] = []
        for line in output.splitlines():
            if not line or "\x00" not in line:
                continue
            name, sha = line.split("\x00", 1)
            if name.endswith("/HEAD"):
                continue
            values.append({"name": name, "sha": sha})
        return values

    def tags(self) -> list[dict[str, str]]:
        output = self.run(
            ["for-each-ref", "--format=%(refname:short)%00%(objectname)", "refs/tags"]
        )
        values: list[dict[str, str]] = []
        for line in output.splitlines():
            if not line or "\x00" not in line:
                continue
            name, sha = line.split("\x00", 1)
            values.append({"name": name, "sha": sha})
        return values

    def add_metadata_exclude(self) -> None:
        git_path = self.run(["rev-parse", "--git-path", "info/exclude"]).strip()
        exclude_path = Path(git_path)
        if not exclude_path.is_absolute():
            exclude_path = self.root / exclude_path
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        entries = {line.strip() for line in existing.splitlines()}
        if ".stress_stack/" in entries:
            return
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        with exclude_path.open("a", encoding="utf-8") as stream:
            stream.write(f"{prefix}.stress_stack/\n")

    def commit_shas(self) -> list[str]:
        return [
            line.strip()
            for line in self.run(["rev-list", "--all", "--topo-order", "--reverse"]).splitlines()
            if line.strip()
        ]

    def commit_records(self) -> list[CommitRecord]:
        shas = self.commit_shas()
        if not shas:
            return []
        worker_count = min(8, max(2, os.cpu_count() or 2))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            return list(executor.map(self._commit_record, shas))

    def _commit_record(self, sha: str) -> CommitRecord:
        format_value = (
            "%H%x00%P%x00%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI%x00"
            "%D%x00%s%x00%b%x00"
        )
        output = self.run_bytes(
            [
                "show",
                "--no-renames",
                "--numstat",
                "-z",
                "--no-color",
                "--no-ext-diff",
                f"--format={format_value}",
                sha,
                "--",
            ],
            record=False,
        )
        parts = output.split(b"\x00", 11)
        if len(parts) != 12:
            raise GitError(f"Could not parse metadata for commit {sha}")
        metadata = [part.decode("utf-8", errors="replace") for part in parts[:11]]
        stats = parts[11]
        file_changes: list[FileChange] = []
        total_additions = 0
        total_deletions = 0
        for raw_entry in stats.split(b"\x00"):
            raw_entry = raw_entry.lstrip(b"\r\n")
            if not raw_entry:
                continue
            columns = raw_entry.split(b"\t", 2)
            if len(columns) != 3:
                continue
            additions_text, deletions_text, path_bytes = columns
            binary = additions_text == b"-" or deletions_text == b"-"
            additions = None if binary else int(additions_text)
            deletions = None if binary else int(deletions_text)
            if additions is not None:
                total_additions += additions
            if deletions is not None:
                total_deletions += deletions
            file_changes.append(
                FileChange(
                    path=path_bytes.decode("utf-8", errors="replace"),
                    additions=additions,
                    deletions=deletions,
                    binary=binary,
                )
            )

        (
            parsed_sha,
            parents,
            author_name,
            author_email,
            authored_at,
            committer_name,
            committer_email,
            committed_at,
            refs_text,
            subject,
            body,
        ) = metadata
        parent_shas = [value for value in parents.split() if value]
        refs = [value.strip() for value in refs_text.split(",") if value.strip()]
        return CommitRecord(
            sha=parsed_sha,
            parent_shas=parent_shas,
            author_name=author_name,
            author_email=author_email,
            authored_at=authored_at,
            committer_name=committer_name,
            committer_email=committer_email,
            committed_at=committed_at,
            refs=refs,
            subject=subject,
            body=body.rstrip("\n"),
            is_merge=len(parent_shas) > 1,
            changed_files=file_changes,
            additions=total_additions,
            deletions=total_deletions,
        )
