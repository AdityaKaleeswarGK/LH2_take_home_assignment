"""A repository built to break this pipeline's assumptions.

glom is a flat-layout, well-docstringed library whose tests are all named
``test_*`` and whose merged pull requests always touch a test file. Every filter
written against it passes on it — which is exactly why passing on it proves
nothing. The brief warns that "hard-coded fixes that only work on the sample
repo will score poorly", so the failure modes are constructed here instead of
hoped for.

Each element below exists to defeat one specific assumption:

* **src/ layout** — module naming that assumes a flat package gets ``src.pkg.core``.
* **No docstrings anywhere** — an excision pre-filter requiring a docstring
  contract returns an empty pool.
* **Exactly one test per function** — a pre-filter requiring two or more
  covering tests returns an empty pool.
* **Real code under ``docs/``** — a hardcoded directory skip-list silently
  discards a genuine source change.
* **A fix for an already-failing test, with the test untouched** — a filter
  requiring the change to modify a test file discards an ideal task: the
  fail-before evidence already exists in the repository.
* **A sweep that rewrites tests without changing behaviour** — the inverse
  case, which must be rejected however good its shape looks.
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import run_git

# No docstrings, deliberately. A repository is not obliged to have them, and a
# pipeline that needs them has a hidden dependency on the sample.
_CORE_V0 = """\
def slugify(value):
    return value.strip().lower().replace(" ", "-")


def truncate(value, limit):
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"
"""

_CORE_V1 = _CORE_V0 + """

def normalize(value, limit=40):
    return truncate(slugify(value), limit)
"""

# render() forgets to escape. The test for that lands in the initial commit and
# fails there: the repository ships a known bug with a test already pinning it.
_RENDER_BUGGY = """\
def render(text):
    return "<p>" + text + "</p>"
"""

_RENDER_FIXED = """\
def render(text):
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return "<p>" + escaped + "</p>"
"""

_TEST_CORE_V0 = """\
from widget.core import slugify, truncate


def test_slugify():
    assert slugify("  Hello World ") == "hello-world"


def test_truncate():
    assert truncate("abcdef", 4) == "abc…"
"""

_TEST_CORE_V1 = _TEST_CORE_V0 + """

def test_normalize():
    assert normalize("  Hello World ", 8) == "hello-w…"
"""

_TEST_RENDER = """\
from widget.render import render


def test_render_wraps():
    assert render("hi") == "<p>hi</p>"


def test_render_escapes():
    assert render("a < b & c") == "<p>a &lt; b &amp; c</p>"
"""

# Real, imported, tested code that happens to live under docs/.
_DOCS_TOOL_V0 = """\
from widget.core import slugify


def anchor(heading):
    return "#" + slugify(heading)
"""

_DOCS_TOOL_V1 = """\
from widget.core import slugify


def anchor(heading, prefix=""):
    base = "#" + slugify(heading)
    return prefix + base if prefix else base
"""

_TEST_DOCS_V0 = """\
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docs"))

from generate import anchor


def test_anchor():
    assert anchor("Getting Started") == "#getting-started"
"""

_TEST_DOCS_V1 = _TEST_DOCS_V0 + """

def test_anchor_with_prefix():
    assert anchor("Getting Started", prefix="doc") == "doc#getting-started"
"""

_PYPROJECT = """\
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "widget"
version = "0.1.0"
requires-python = ">=3.12"

[tool.setuptools.packages.find]
where = ["src"]
"""


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _merge(repository: Path, branch: str, subject: str, number: int) -> str:
    """Land a branch as a merge commit, the way a merged pull request does."""
    run_git(repository, "checkout", "-q", "main")
    run_git(repository, "merge", "--no-ff", branch, "-m", f"Merge pull request #{number} from x/{branch}\n\n{subject}")
    return run_git(repository, "rev-parse", "HEAD")


def build_adversarial_repository(root: Path) -> dict[str, str]:
    """Create the repository and its ingest artifacts. Returns pr -> merge sha."""
    root.mkdir(parents=True, exist_ok=True)
    run_git(root, "init", "-q", "-b", "main")
    run_git(root, "config", "user.name", "Stress Stack Test")
    run_git(root, "config", "user.email", "stress-stack@example.test")

    _write(root, "pyproject.toml", _PYPROJECT)
    _write(root, "src/widget/__init__.py", "")
    _write(root, "src/widget/core.py", _CORE_V0)
    _write(root, "src/widget/render.py", _RENDER_BUGGY)
    _write(root, "docs/generate.py", _DOCS_TOOL_V0)
    _write(root, "tests/test_core.py", _TEST_CORE_V0)
    _write(root, "tests/test_render.py", _TEST_RENDER)
    _write(root, "tests/test_docs.py", _TEST_DOCS_V0)
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "Initial commit")

    shas: dict[str, str] = {}

    # PR#1 — an ordinary feature: new function, new test. The control case.
    run_git(root, "checkout", "-q", "-b", "feature-normalize")
    _write(root, "src/widget/core.py", _CORE_V1)
    _write(root, "tests/test_core.py", _TEST_CORE_V1.replace(
        "from widget.core import slugify, truncate",
        "from widget.core import normalize, slugify, truncate",
    ))
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "Add normalize")
    shas["1"] = _merge(root, "feature-normalize", "Add normalize", 1)

    # PR#2 — fixes a bug whose test already exists and already fails. Touches no
    # test file at all, so a filter requiring a changed test discards it.
    run_git(root, "checkout", "-q", "-b", "fix-escaping")
    _write(root, "src/widget/render.py", _RENDER_FIXED)
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "Escape HTML in render")
    shas["2"] = _merge(root, "fix-escaping", "Escape HTML in render", 2)

    # PR#3 — documentation prose only. Must be rejected, but by measurement.
    run_git(root, "checkout", "-q", "-b", "docs-prose")
    _write(root, "README.md", "# widget\n\nA small library.\n")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "Add README")
    shas["3"] = _merge(root, "docs-prose", "Add README", 3)

    # PR#4 — a real source change that lives under docs/. A directory skip-list
    # throws this away as documentation.
    run_git(root, "checkout", "-q", "-b", "docs-anchor-prefix")
    _write(root, "docs/generate.py", _DOCS_TOOL_V1)
    _write(root, "tests/test_docs.py", _TEST_DOCS_V1)
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "Support anchor prefixes")
    shas["4"] = _merge(root, "docs-anchor-prefix", "Support anchor prefixes", 4)

    # PR#5 — a modernisation sweep. Rewrites test bodies, changes no behaviour.
    run_git(root, "checkout", "-q", "-b", "modernise")
    _write(root, "src/widget/core.py", _CORE_V1.replace("value.strip()", "str(value).strip()"))
    _write(
        root,
        "tests/test_core.py",
        _TEST_CORE_V1.replace(
            "from widget.core import slugify, truncate",
            "from widget.core import normalize, slugify, truncate",
        ).replace('== "hello-world"', "== str('hello-world')"),
    )
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "Modernise")
    shas["5"] = _merge(root, "modernise", "Modernise", 5)

    _write_history(root, shas)
    return shas


def _write_history(root: Path, shas: dict[str, str]) -> None:
    titles = {
        "1": ("Add normalize", "Adds a combined slug-and-truncate helper."),
        "2": ("Escape HTML in render", "render() emitted raw text; escape it."),
        "3": ("Add README", "Documentation only."),
        "4": ("Support anchor prefixes", "anchor() gains an optional prefix."),
        "5": ("Modernise", "Cosmetic cleanup, no behaviour change."),
    }
    history = root / ".stress_stack" / "history"
    history.mkdir(parents=True, exist_ok=True)

    (history / "pull_requests.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "number": int(number),
                    "title": titles[number][0],
                    "body": titles[number][1],
                    "state": "closed",
                    "merged_at": f"2024-0{number}-01T00:00:00Z",
                    "merge_commit_sha": sha,
                    "html_url": f"https://example.test/pull/{number}",
                    "author": "someone",
                },
                sort_keys=True,
            )
            + "\n"
            for number, sha in sorted(shas.items())
        ),
        encoding="utf-8",
    )
    (history / "commits.jsonl").write_text(
        "".join(json.dumps({"sha": sha}, sort_keys=True) + "\n" for sha in sorted(shas.values())),
        encoding="utf-8",
    )
    (history / "commit_pr_links.jsonl").write_text(
        "".join(
            json.dumps(
                {"commit_sha": sha, "pr_number": int(number), "method": "github_merge_sha"},
                sort_keys=True,
            )
            + "\n"
            for number, sha in sorted(shas.items())
        ),
        encoding="utf-8",
    )
