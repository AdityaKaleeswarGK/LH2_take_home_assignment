"""The instruction must be usable, neutral, and provably free of the answer."""

from __future__ import annotations

from stress_stack.instruct import (
    behaviour_summary,
    build_evidence,
    leak_check,
    mechanical_instruction,
    sanitize,
    solution_only_names,
)

DIFF = """\
--- a/glom/streaming.py
+++ b/glom/streaming.py
@@ -1,3 +1,9 @@
+class Iter:
+    def __init__(self, subspec, **kwargs):
+        self.subspec = subspec
+        self._scope = kwargs.pop('scope', None)
+
+def _chunked(source, size):
+    return [source[i:i + size] for i in range(0, len(source), size)]
"""


def test_a_pull_request_body_that_pastes_the_patch_is_stripped() -> None:
    """Authors paste patches into descriptions constantly."""
    body = (
        "Adds streaming support.\n\n"
        "```python\n"
        "def _chunked(source, size):\n"
        "    return [source[i:i + size]]\n"
        "```\n\n"
        "See #42 at https://example.test/x for discussion.\n"
        "+    self._scope = kwargs.pop('scope', None)\n"
    )
    cleaned = sanitize(body)

    assert "Adds streaming support." in cleaned
    assert "_chunked" not in cleaned
    assert "kwargs.pop" not in cleaned
    assert "https://" not in cleaned


def test_solution_only_names_are_computed_against_the_base_graph() -> None:
    """A name the diff merely *calls* is not a name the diff invented."""
    names = solution_only_names(DIFF, base_names={"range", "len"})

    assert "Iter" in names
    assert "_chunked" in names
    assert "range" not in names and "len" not in names


def test_copied_code_is_caught() -> None:
    instruction = (
        "Implement chunking. Use return [source[i:i + size] for i in range(0, len(source), size)]."
    )
    report = leak_check(instruction, DIFF, base_names={"range", "len"})

    assert report.copied_spans
    assert report.clean is False


def test_a_private_name_from_the_solution_is_a_leak() -> None:
    """Naming internal structure hands over a design decision."""
    report = leak_check(
        "Add a helper called _chunked that splits the source.", DIFF, base_names=set()
    )

    assert report.private_names == ["_chunked"]
    assert report.clean is False


def test_naming_a_new_public_api_is_permitted_and_recorded() -> None:
    """The brief allows the name and forbids the implementation."""
    report = leak_check(
        "Add a spec type named Iter that lazily evaluates over an iterable.",
        DIFF,
        base_names=set(),
    )

    assert report.public_names == ["Iter"]
    assert report.private_names == []
    assert report.clean is True


def test_a_behavioural_instruction_passes_the_check() -> None:
    report = leak_check(
        "Iteration over a target must yield elements one at a time rather than "
        "materialising the whole sequence, and must stop when the source is exhausted.",
        DIFF,
        base_names=set(),
    )

    assert report.clean is True


def test_behaviour_summary_reads_as_behaviour_not_as_node_ids() -> None:
    text = behaviour_summary(
        ["glom.test.test_x::test_path_star_support", "glom.test.test_x::test_assign_missing"]
    )

    assert "test_" not in text
    assert "path star support" in text
    assert "assign missing" in text


def excision_task() -> dict:
    return {
        "task_id": "excise-x",
        "source": "excision",
        "subject": "glom/grouping.py::glom.grouping.GROUP",
        "primary_module": "glom.grouping",
        "targets": ["glom.test.test_grouping::test_bucketing"],
        "files_in_scope": ["glom/grouping.py"],
    }


def test_the_mechanical_excision_instruction_is_self_contained_and_neutral() -> None:
    task = excision_task()
    evidence = build_evidence(
        task,
        module_purpose="glom.grouping buckets and aggregates target data",
        signature="GROUP(target, spec, scope)",
        docstring="Group values from the target according to the spec.",
        qualified_name="glom.grouping.GROUP",
    )
    written = mechanical_instruction(task, evidence)

    assert "GROUP" in written["title"]
    assert "Group values from the target" in written["instruction"]
    assert "bucketing" in written["instruction"]
    # Neutrality: it must not prescribe an approach.
    assert "different but correct" in written["instruction"] or "not how you achieve it" in written["instruction"]
    # Self-contained: it says how success is measured.
    assert "test suite" in written["instruction"]


def test_the_mechanical_instruction_cannot_leak_because_it_never_sees_the_diff() -> None:
    task = excision_task()
    written = mechanical_instruction(task, build_evidence(task))

    assert leak_check(written["instruction"], DIFF, base_names=set()).clean is True


def test_a_history_instruction_uses_sanitised_prose() -> None:
    task = {
        "task_id": "pr-100",
        "source": "history",
        "title": "Streaming",
        "primary_module": "glom.streaming",
        "targets": ["glom.test.test_streaming::test_iter_basic"],
        "files_in_scope": ["glom/streaming.py"],
    }
    evidence = build_evidence(
        task,
        module_purpose="glom.streaming evaluates specs lazily over iterables",
        pr_body="Adds lazy iteration.\n```python\ndef _chunked(a, b):\n    pass\n```",
    )
    written = mechanical_instruction(task, evidence)

    assert "Adds lazy iteration." in written["instruction"]
    assert "_chunked" not in written["instruction"]
    assert "iter basic" in written["instruction"]
