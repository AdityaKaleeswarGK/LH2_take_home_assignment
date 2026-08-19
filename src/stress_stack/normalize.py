"""One place that decides what varies between two runs and says nothing.

Two comparisons in this pipeline ask the same question of a pair of test
outputs — *is this the same run twice?* — and both answered it with their own
copy of the same regex table: ``hygiene_verify`` comparing a tree before and
after formatting, and ``container_doctor`` comparing an image against itself.
Keeping two copies is what made the line-ordering bug need fixing twice, once
in each, weeks apart. It is fixed here once.

The two are not identical, and the difference is the whole reason this module
takes an argument rather than exporting a single function:

* **Durations, addresses, temporary paths and timestamps are noise for both.**
  They differ between two runs of an unchanged suite, so leaving them in makes
  every comparison fail.
* **Source locations are noise for hygiene and evidence for the container.**
  Hygiene's entire job is to move lines — reformatting ``if x { t.Fatal() }``
  onto three lines renames ``calc_test.go:10`` to ``:11`` in every failure
  message — so an unnormalised comparison reads a successful format as a
  regression and reverts it. Two runs of an *unchanged* tree have no such
  excuse, so the determinism gate must keep seeing line numbers or it stops
  measuring determinism.

Ordering is stripped for both. A suite using parallel tests emits its lines in
scheduling order — Go's ``-v`` interleaves ``=== RUN`` / ``=== PAUSE`` /
``=== CONT`` differently every time — so an ordered comparison reports a
regression for a tree nobody touched. Measured on spf13/cast: two runs of the
same image produced 32233 identical lines in a different sequence, and hygiene
reverted its own formatting over it. Comparing the multiset still catches a
test that flips, an assertion that moves, and a line that appears or vanishes.
It only stops catching pure reordering, which is the scheduler rather than the
code.
"""

from __future__ import annotations

import re

# Terminal control sequences. Stripped before anything else, because they do not
# only add noise — they *split the values underneath them*. vitest writes a
# duration as `\x1b[90m 12\x1b[2mms\x1b[22m`, so `12` and `ms` are separated by
# an escape and no `\d+ ?ms` pattern can match it. The duration then survived
# normalisation, differed between two runs of an unchanged image, and the
# determinism gate called a passing suite nondeterministic.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

# Volatile between two runs of the same thing, in every ecosystem. Normalising
# more than this would start hiding real disagreement.
_COMMON: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b\d+\.\d+s\b"), "<time>"),
    (re.compile(r"\b\d+(\.\d+)? ?ms\b"), "<time>"),
    (re.compile(r"0x[0-9a-fA-F]+"), "<addr>"),
    (re.compile(r"/tmp/[^\s\"']+"), "<tmp>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*"), "<timestamp>"),
    # A bare wall-clock time, with no date in front of it. vitest ends every run
    # with `Start at  13:39:04`, which differs between any two runs — so an
    # unchanged suite read as a regression and hygiene reverted its own
    # formatting over it. The date-qualified pattern above cannot match this.
    (re.compile(r"\b\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\b"), "<clock>"),
)

# `path/file.go:12:34:` and `path/file.go:12:` -> `path/file.go:<line>`
_SOURCE_LOCATION: tuple[re.Pattern[str], str] = (
    re.compile(
        r"([\w./\\-]+\.(?:go|rs|py|ts|tsx|js|jsx|mjs|cjs|c|cc|cpp|h|hpp)):\d+(?::\d+)?"
    ),
    r"\1:<line>",
)


def normalize(text: str, *, source_locations: bool = False) -> str:
    """Reduce runner output to what a difference between two runs would mean.

    Set ``source_locations`` when the comparison spans a change that is allowed
    to move lines — that is, hygiene. Leave it off when the two sides are meant
    to be byte-identical trees, which is what the determinism gate checks.
    """
    text = _ANSI.sub("", text)
    patterns = (*_COMMON, _SOURCE_LOCATION) if source_locations else _COMMON
    for pattern, replacement in patterns:
        text = pattern.sub(replacement, text)
    return "\n".join(sorted(line.rstrip() for line in text.strip().splitlines()))
