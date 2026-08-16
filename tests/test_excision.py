"""Adversarial layouts for body removal.

Every case here is a way a Python file can be shaped that would let a naive
line-slice corrupt it or leak the answer. glom exhibits almost none of them,
which is exactly why they are written by hand.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from stress_stack.excision import (
    EXPLICIT,
    NEUTRAL,
    ExcisionError,
    apply_to_tree,
    excise,
    plan_excision,
)


def line_of(source: str, name: str) -> int:
    """The ``def`` line the graph would anchor, found the same way the graph finds it."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node.lineno
    raise AssertionError(f"no definition named {name}")


def cut(source: str, name: str, *, strategy: str = EXPLICIT):
    return excise(source, name, line_of(source, name), strategy=strategy, path="m.py")


DECORATED = '''\
import functools


class Registry:
    """A registry."""

    @property
    @functools.lru_cache(maxsize=None)
    def entries(self):
        """The registered entries, in insertion order."""
        collected = []
        for key in self._keys:
            collected.append(self._values[key])
        return collected

    def other(self):
        return 1
'''


def test_decorators_and_docstring_survive() -> None:
    result = cut(DECORATED, "entries")

    assert "@property" in result.stubbed
    assert "@functools.lru_cache(maxsize=None)" in result.stubbed
    assert '"""The registered entries, in insertion order."""' in result.stubbed
    assert "self._values[key]" not in result.stubbed
    # The neighbouring method must be untouched.
    assert "    def other(self):\n        return 1\n" in result.stubbed
    assert result.plan.decorators == ["property", "functools.lru_cache(maxsize=None)"]


def test_stub_keeps_the_file_parsable_and_indented() -> None:
    result = cut(DECORATED, "entries")
    ast.parse(result.stubbed)
    assert "        raise NotImplementedError('entries is not implemented')" in result.stubbed


MULTILINE_SIGNATURE = '''\
def combine(
    left: int,
    right: int = 3,
    *,
    scale: float = 1.0,
) -> float:
    """Combine two numbers."""
    total = left + right
    return total * scale
'''


def test_multiline_signature_is_preserved_whole() -> None:
    result = cut(MULTILINE_SIGNATURE, "combine")

    assert "scale: float = 1.0," in result.stubbed
    assert ") -> float:" in result.stubbed
    assert "total = left + right" not in result.stubbed
    ast.parse(result.stubbed)


ONE_LINER = "def quick(value): return value * 2\n"

INLINE_AFTER_MULTILINE = '''\
def sneaky(
    value,
): return value * 2
'''


@pytest.mark.parametrize("source, name", [(ONE_LINER, "quick"), (INLINE_AFTER_MULTILINE, "sneaky")])
def test_body_on_the_signature_line_is_refused_not_mangled(source: str, name: str) -> None:
    """Refusing costs a candidate. Slicing the lines costs the file."""
    with pytest.raises(ExcisionError, match="signature line"):
        cut(source, name)


ASYNC_SOURCE = '''\
import asyncio


async def fetch(url):
    """Fetch a URL and return its body."""
    await asyncio.sleep(0)
    return url.upper()
'''


def test_async_definitions_are_supported() -> None:
    result = cut(ASYNC_SOURCE, "fetch")

    assert result.plan.is_async is True
    assert result.plan.is_generator is False
    assert "async def fetch(url):" in result.stubbed
    assert "url.upper()" not in result.stubbed


GENERATOR = '''\
def walk(tree):
    """Yield every node in the tree."""
    for child in tree:
        yield child
'''

NESTED_YIELD = '''\
def build(tree):
    """Return a generator over the tree."""

    def inner():
        for child in tree:
            yield child

    return inner()
'''


def test_a_generator_only_offers_the_explicit_strategy() -> None:
    """A neutral body silently stops it being a generator, and the caller breaks."""
    plan = plan_excision(GENERATOR, "walk", line_of(GENERATOR, "walk"))

    assert plan.is_generator is True
    assert plan.strategies() == [EXPLICIT]


def test_a_yield_in_a_nested_function_does_not_make_the_outer_one_a_generator() -> None:
    plan = plan_excision(NESTED_YIELD, "build", line_of(NESTED_YIELD, "build"))

    assert plan.is_generator is False
    assert plan.strategies() == [NEUTRAL, EXPLICIT]


TRAILING_COMMENT = '''\
def resolve(value):
    """Resolve a value."""
    if value is None:
        return 0
    return value
    # Uses the two-pass approach because the single-pass one loses precision.
    # See issue #42.


def next_function():
    # This comment introduces the next function and must survive.
    return 1
'''


def test_comments_inside_the_removed_body_do_not_leak() -> None:
    """A comment left behind explains the algorithm that was just removed."""
    result = cut(TRAILING_COMMENT, "resolve")

    assert "two-pass approach" not in result.stubbed
    assert "issue #42" not in result.stubbed
    # The comment belonging to the next definition is at its indentation and stays.
    assert "This comment introduces the next function and must survive." in result.stubbed
    ast.parse(result.stubbed)


SAME_NAME_TWICE = '''\
class First:
    def run(self):
        """First implementation."""
        return "first-body"


class Second:
    def run(self):
        """Second implementation."""
        return "second-body"
'''


def test_the_anchor_line_disambiguates_same_named_methods() -> None:
    tree = ast.parse(SAME_NAME_TWICE)
    lines = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "run"
    ]
    second = excise(SAME_NAME_TWICE, "run", max(lines), strategy=EXPLICIT, path="m.py")

    assert "first-body" in second.stubbed
    assert "second-body" not in second.stubbed


def test_the_dotted_path_disambiguates_without_a_line_number() -> None:
    """The graph parses the working tree; tasks are built from a git archive."""
    second = excise(SAME_NAME_TWICE, "run", strategy=EXPLICIT, path="m.py", dotted="Second.run")

    assert "first-body" in second.stubbed
    assert "second-body" not in second.stubbed


def test_the_dotted_path_survives_the_file_moving() -> None:
    """Reformatting shifts every line; the dotted path must still resolve."""
    shifted = "# a comment ruff would add or remove\n" * 12 + SAME_NAME_TWICE
    result = excise(shifted, "run", strategy=EXPLICIT, path="m.py", dotted="First.run")

    assert "second-body" in result.stubbed
    assert "first-body" not in result.stubbed


OVERLOADED = '''\
import typing


@typing.overload
def render(value: int) -> str: ...


@typing.overload
def render(value: str) -> str: ...


def render(value):
    """Render a value."""
    return str(value)
'''


def test_a_redefined_function_is_refused_rather_than_guessed() -> None:
    """Three definitions of one name: no single body is "the" body."""
    with pytest.raises(ExcisionError, match="defined 3 times"):
        excise(OVERLOADED, "render", strategy=EXPLICIT, path="m.py", dotted="render")


NO_DOCSTRING = '''\
def add(a, b):
    total = a + b
    return total
'''


def test_a_function_without_a_docstring_still_excises() -> None:
    result = cut(NO_DOCSTRING, "add", strategy=NEUTRAL)

    assert result.plan.has_docstring is False
    assert "def add(a, b):\n    return None\n" == result.stubbed


DOCSTRING_ONLY = '''\
def todo():
    """Not written yet."""
'''


def test_a_body_that_is_only_a_docstring_is_refused() -> None:
    with pytest.raises(ExcisionError, match="no implementation"):
        cut(DOCSTRING_ONLY, "todo")


MULTILINE_DOCSTRING = '''\
def parse(text):
    """Parse text.

    Longer explanation that spans
    several lines and must survive intact.
    """
    cleaned = text.strip()
    return cleaned
'''


def test_a_multiline_docstring_survives_intact() -> None:
    result = cut(MULTILINE_DOCSTRING, "parse")

    assert "several lines and must survive intact." in result.stubbed
    assert "text.strip()" not in result.stubbed
    ast.parse(result.stubbed)


def test_crlf_files_keep_their_line_endings() -> None:
    source = NO_DOCSTRING.replace("\n", "\r\n")
    result = excise(source, "add", 1, strategy=NEUTRAL, path="m.py")

    assert "\r\n" in result.stubbed
    assert "\n" not in result.stubbed.replace("\r\n", "")


def test_the_diff_restores_exactly_what_was_removed() -> None:
    result = cut(DECORATED, "entries")
    diff = result.diff()

    assert diff.startswith("--- a/m.py")
    assert "+        collected = []" in diff
    assert "-        raise NotImplementedError" in diff


def test_applying_to_a_tree_writes_only_the_target_file(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text(NO_DOCSTRING, encoding="utf-8")
    (tmp_path / "pkg" / "other.py").write_text("UNTOUCHED = 1\n", encoding="utf-8")
    result = excise(NO_DOCSTRING, "add", 1, strategy=NEUTRAL, path="pkg/m.py")

    apply_to_tree(tmp_path, result)

    assert (tmp_path / "pkg" / "m.py").read_text() == result.stubbed
    assert (tmp_path / "pkg" / "other.py").read_text() == "UNTOUCHED = 1\n"


def test_a_missing_target_file_is_an_error(tmp_path: Path) -> None:
    result = excise(NO_DOCSTRING, "add", 1, strategy=NEUTRAL, path="pkg/m.py")
    with pytest.raises(ExcisionError, match="missing"):
        apply_to_tree(tmp_path, result)
