"""Verify that hygiene changed formatting and nothing else, inside a container.

Python's hygiene stage has always compared a before/after test snapshot and
reverted on regression. Every other ecosystem formatted the tree and asserted
nothing, because there was no equivalent harness — which is why those results
reported ``regressions_verified=False``.

This runs the project's own test command in the ecosystem's container, once
before hygiene and once after, and compares the two. Doing it in a container
rather than on the host is the same decision the ``validate`` stage already
makes: the repository's build and test code is untrusted, and a formatter run
that provokes it should not be able to reach the developer's machine.

A regression reverts the formatting. Reformatting is never worth a behaviour
change, so the safe outcome is the unformatted tree plus a recorded reason.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stress_stack.atomic import atomic_write_text
from stress_stack.tooling import run

# What legitimately differs between two runs, so the comparison is about
# behaviour rather than incidentals.
#
# Source locations are normalised here and *not* in the container doctor's
# determinism check, and the difference is deliberate. Two runs of the same tree
# must produce identical line numbers, so a change there is real. But hygiene's
# whole job is to move lines: reformatting `if x { t.Fatal() }` onto three lines
# renames `calc_test.go:10` to `calc_test.go:11` in every failure message. Left
# unnormalised, a successful format reads as a regression and reverts itself.
_VOLATILE: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b\d+\.\d+s\b"), "<time>"),
    (re.compile(r"\b\d+(\.\d+)? ?ms\b"), "<time>"),
    (re.compile(r"0x[0-9a-fA-F]+"), "<addr>"),
    (re.compile(r"/tmp/[^\s\"']+"), "<tmp>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*"), "<timestamp>"),
    # `path/file.go:12:34:` and `path/file.go:12:` -> `path/file.go:<line>`
    (
        re.compile(
            r"([\w./\\-]+\.(?:go|rs|py|ts|tsx|js|jsx|mjs|cjs|c|cc|cpp|h|hpp)):\d+(?::\d+)?"
        ),
        r"\1:<line>",
    ),
)

AVAILABLE = "available"
UNAVAILABLE = "unavailable"


@dataclass
class SuiteSnapshot:
    status: str
    exit_code: int | None
    normalized: str
    reason: str = ""

    @property
    def usable(self) -> bool:
        return self.status == AVAILABLE


def _normalize(text: str) -> str:
    """Strip what varies between two runs of an unchanged suite.

    Order is one of those things. A suite using parallel tests emits its lines
    in scheduling order — Go's `-v` output interleaves `=== RUN` / `=== PAUSE` /
    `=== CONT` differently every time — so an ordered comparison reports a
    regression for a tree nobody touched. Measured on spf13/cast: two runs of
    the same image produced 32233 identical lines in a different sequence, and
    hygiene reverted its own formatting over it.

    Comparing the multiset of lines still catches every change that matters —
    a test that starts failing, an assertion message that moves, a line that
    appears or disappears. It only stops catching pure reordering, which is
    exactly the thing that is not evidence.
    """
    for pattern, replacement in _VOLATILE:
        text = pattern.sub(replacement, text)
    return "\n".join(sorted(text.strip().splitlines()))


def build_probe_image(root: Path, profile: Any) -> tuple[str | None, str]:
    """Build a throwaway image used only for the before/after comparison."""
    if not shutil.which("docker"):
        return None, "docker_not_installed"

    from stress_stack.container_doctor import pin_base_image, synthesize_dockerfile

    evidence = root / ".stress_stack" / "hygiene"
    evidence.mkdir(parents=True, exist_ok=True)
    base_reference, _ = pin_base_image(profile.base_image)
    dockerfile = evidence / "Dockerfile.probe"
    atomic_write_text(dockerfile, synthesize_dockerfile(profile, base_reference))

    tag = f"stress-stack/{root.name.lower()}:hygiene-probe"
    build = run(
        ["docker", "build", "-t", tag, "-f", str(dockerfile), str(root)],
        timeout=3600.0,
    )
    atomic_write_text(evidence / "probe_build.log", build.stdout + build.stderr)
    if not build.ok:
        tail = "\n".join((build.stdout + build.stderr).strip().splitlines()[-5:])
        return None, f"probe_image_build_failed: {tail[:300]}"
    return tag, ""


def snapshot_suite(image: str) -> SuiteSnapshot:
    """Run the image's own test command under the standard lockdown."""
    result = run(
        ["docker", "run", "--rm", "--network", "none", "--cap-drop", "ALL", image],
        timeout=1800.0,
    )
    if result.exit_code == 124:
        return SuiteSnapshot(UNAVAILABLE, None, "", "suite_timed_out")
    return SuiteSnapshot(
        status=AVAILABLE,
        exit_code=result.exit_code,
        normalized=_normalize(result.stdout + result.stderr),
    )


def compare(before: SuiteSnapshot, after: SuiteSnapshot) -> tuple[bool, str]:
    """Did hygiene change observable behaviour? Returns (regressed, reason)."""
    if not before.usable or not after.usable:
        return False, "not_compared: a snapshot was unavailable"
    if before.exit_code != after.exit_code:
        return True, f"suite exit code moved {before.exit_code} -> {after.exit_code}"
    if before.normalized != after.normalized:
        return True, "suite output changed after formatting"
    return False, ""


def revert_working_tree(root: Path) -> bool:
    """Undo formatting edits to tracked files, leaving new config files alone.

    New linter and formatter configuration is a deliverable the brief asks for,
    and it cannot be the cause of a behaviour change on its own — only the
    rewritten sources can. So the revert is scoped to tracked modifications.
    """
    result = run(["git", "checkout", "--", "."], cwd=root, timeout=300.0)
    return result.ok
