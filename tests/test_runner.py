"""Node id translation between the three namespaces a task touches.

JUnit reports a dotted ``classname``. coverage contexts are path-addressed.
pytest's command line wants a path with ``::`` separators. Getting the wrong
one is silent: the ids simply fail to resolve, and a candidate is rejected for
"no target test" when the real cause is a broken translation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stress_stack.runner import (
    forget_source_roots,
    pytest_argument,
    pytest_arguments,
    source_roots,
)


@pytest.fixture(autouse=True)
def _clean_source_roots_cache():
    """The cache is process-wide; no test may inherit another's entries."""
    forget_source_roots()
    yield
    forget_source_roots()


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


# --------------------------------------------------------------------------
# PYTHONPATH derivation, and the cache in front of it
# --------------------------------------------------------------------------


def test_a_flat_layout_resolves_to_the_mount_alone(tree: Path) -> None:
    assert source_roots(tree) == "/work"


def test_a_src_layout_outranks_the_repository_root(tmp_path: Path) -> None:
    for relative in ("src/pkg/__init__.py", "src/pkg/core.py", "tests/test_core.py"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    assert source_roots(tmp_path) == "/work/src:/work"


def test_a_rewritten_tree_is_not_served_from_the_cache(tmp_path: Path) -> None:
    """The evaluation tree is rebuilt in place after its first run.

    Without invalidation the second run would be handed the PYTHONPATH of a
    tree that no longer exists, which is the silent-wrong-answer failure the
    src-layout comment in `source_roots` describes.
    """
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    assert source_roots(tmp_path) == "/work"

    # Re-lay the same tree as a src layout, exactly as `build_evaluation_tree`
    # would when the verifier overlay adds files.
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "pkg").mkdir()
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")

    assert source_roots(tmp_path) == "/work", "expected the stale cached value"
    forget_source_roots()
    assert source_roots(tmp_path) == "/work/src:/work"


def test_the_cache_returns_the_same_answer_for_the_same_tree(tmp_path: Path) -> None:
    for relative in ("src/pkg/__init__.py", "src/pkg/core.py"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    first = source_roots(tmp_path)
    assert source_roots(tmp_path) == first
    # An unresolved path must hit the same entry, or a candidate's eight runs
    # would each pay for the walk.
    assert source_roots(tmp_path / "." ) == first


def test_a_host_mount_is_still_a_mount(tmp_path: Path) -> None:
    """The mount is a parameter, so the same derivation can serve a host run."""
    for relative in ("src/pkg/__init__.py", "src/pkg/core.py"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    assert source_roots(tmp_path, mount="/mnt") == "/mnt/src:/mnt"
