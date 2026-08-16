"""Node id translation between the three namespaces a task touches.

JUnit reports a dotted ``classname``. coverage contexts are path-addressed.
pytest's command line wants a path with ``::`` separators. Getting the wrong
one is silent: the ids simply fail to resolve, and a candidate is rejected for
"no target test" when the real cause is a broken translation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stress_stack.runner import pytest_argument, pytest_arguments


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    for relative in (
        "glom/__init__.py",
        "glom/test/__init__.py",
        "glom/test/test_basic.py",
        "tests/test_flat.py",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    return tmp_path


def test_dotted_junit_id_resolves_to_a_path(tree: Path) -> None:
    assert (
        pytest_argument(tree, "glom.test.test_basic::test_call")
        == "glom/test/test_basic.py::test_call"
    )


def test_dotted_junit_id_with_a_class_splits_at_the_file(tree: Path) -> None:
    assert (
        pytest_argument(tree, "glom.test.test_basic.TestSpec::test_call")
        == "glom/test/test_basic.py::TestSpec::test_call"
    )


def test_a_path_addressed_coverage_context_is_left_alone(tree: Path) -> None:
    """Splitting this on '.' shears '.py' into a class that does not exist."""
    node_id = "glom/test/test_basic.py::test_call"
    assert pytest_argument(tree, node_id) == node_id


def test_a_path_addressed_context_with_a_class_is_left_alone(tree: Path) -> None:
    node_id = "tests/test_flat.py::TestThing::test_call"
    assert pytest_argument(tree, node_id) == node_id


def test_an_unknown_module_is_returned_unchanged(tree: Path) -> None:
    """Better to hand pytest something it rejects than to invent a path."""
    assert pytest_argument(tree, "not.a.module::test_x") == "not.a.module::test_x"


def test_an_id_without_a_separator_is_untouched(tree: Path) -> None:
    assert pytest_argument(tree, "test_call") == "test_call"


def test_arguments_translate_as_a_batch(tree: Path) -> None:
    assert pytest_arguments(
        tree, ["glom.test.test_basic::a", "glom/test/test_basic.py::b"]
    ) == ["glom/test/test_basic.py::a", "glom/test/test_basic.py::b"]
    assert pytest_arguments(tree, None) == []
