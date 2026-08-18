from __future__ import annotations

from pathlib import Path

from stress_stack import coverage_map as cm
from stress_stack.graph import build_graph

_SOURCE = '''"""Module."""


class Engine:
    """An engine."""

    def run(self, value):
        """Run it."""
        total = value
        total += 1
        total += 2
        return total

    def idle(self):
        """Do nothing."""
        return None


def helper(x):
    """Help."""
    a = x
    b = a + 1
    c = b + 1
    return c
'''


def repository(root: Path) -> None:
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "mod.py").write_text(_SOURCE, encoding="utf-8")


def test_context_ids_normalize_onto_pytest_node_ids() -> None:
    assert cm.normalize_context("pkg.test_mod.test_name") == "pkg.test_mod::test_name"
    assert cm.normalize_context("pkg.test_mod.test_name|setup") == "pkg.test_mod::test_name"
    assert cm.normalize_context("pkg.test_mod::test_name") == "pkg.test_mod::test_name"
    assert cm.normalize_context("") == ""


def test_lines_attribute_to_the_innermost_symbol(tmp_path: Path) -> None:
    """A method's lines sit inside its class too; the class must not absorb them."""
    repository(tmp_path)
    graph = build_graph(tmp_path)
    lines = {"pkg/mod.py": {8: ["t.test_a.test_run"], 9: ["t.test_a.test_run"]}}

    result = cm.build(graph, lines)
    run = result.symbols["pkg/mod.py::pkg.mod.Engine.run"]
    engine = result.symbols["pkg/mod.py::pkg.mod.Engine"]

    assert run.covered_lines == 2
    assert engine.covered_lines == 0
    assert run.covering_tests == ["t.test_a::test_run"]


def test_uncovered_symbols_are_persisted_for_test_generation(tmp_path: Path) -> None:
    repository(tmp_path)
    graph = build_graph(tmp_path)
    result = cm.build(graph, {"pkg/mod.py": {8: ["t.test_a.test_run"]}})

    assert "pkg/mod.py::pkg.mod.helper" in result.symbols
    assert "pkg/mod.py::pkg.mod.helper" in result.to_dict()["uncovered_symbols"]


def covered(
    tests: int,
    *,
    ratio: float = 0.8,
    body: int = 10,
    doc: bool = True,
    name: str = "fn",
    ceiling: int = 12,
) -> cm.CoveredSymbol:
    symbol = cm.CoveredSymbol(
        symbol_id=f"f.py::m.{name}",
        path="f.py",
        kind="function",
        qualified_name=f"m.{name}",
        body_lines=body,
        covered_lines=int(body * ratio),
        has_docstring=doc,
        covering_ceiling=ceiling,
    )
    symbol.covering_tests = [f"t::{name}_{i}" for i in range(tests)]
    return symbol


def test_excision_band_rejects_both_extremes() -> None:
    """One test under-constrains; a hundred means the symbol is infrastructure."""
    assert covered(1).excision_ready is False
    assert covered(3).excision_ready is True
    assert covered(12).excision_ready is True
    assert covered(60).excision_ready is False


def test_excision_requires_a_contract_and_real_coverage() -> None:
    assert covered(3, doc=False).excision_ready is False
    assert covered(3, ratio=0.3).excision_ready is False
    assert covered(3, body=2).excision_ready is False


def test_focus_score_prefers_well_covered_but_not_central() -> None:
    focused = covered(3, ratio=0.9)
    central = covered(12, ratio=0.9)

    assert focused.focus_score > central.focus_score
    # Infrastructure ranks below both, but is not scored out of existence: on a
    # repository where everything is broadly covered it is all there is.
    assert covered(60, ratio=0.9).focus_score < central.focus_score
    assert covered(60, ratio=0.9).focus_score > 0.0


def test_a_symbol_without_a_docstring_still_scores() -> None:
    """A repository that docstrings nothing must not yield an empty pool."""
    documented = covered(3, name="a")
    bare = covered(3, doc=False, name="b")

    assert bare.excision_possible is True
    assert 0.0 < bare.focus_score < documented.focus_score


def test_a_single_covering_test_still_scores() -> None:
    """One test per function is a testing style, not an absence of tasks."""
    lonely = covered(1, name="a")
    paired = covered(3, name="b")

    assert lonely.excision_possible is True
    assert 0.0 < lonely.focus_score < paired.focus_score


def test_a_non_callable_is_the_one_thing_that_cannot_be_excised() -> None:
    symbol = covered(3)
    object.__setattr__(symbol, "kind", "class")
    assert symbol.excision_possible is False
    assert symbol.focus_score == 0.0


def calibrated(counts: list[int]) -> cm.CoverageMap:
    """A map whose only meaningful property is its covering-test distribution.

    Each symbol gets its own test ids, so the suite size the calibration reads
    is the sum rather than the maximum — as it is in a real repository.
    """
    coverage_map = cm.CoverageMap(status="available")
    for index, count in enumerate(counts):
        symbol = covered(count, name=f"f{index}")
        coverage_map.symbols[symbol.symbol_id] = symbol
    coverage_map.calibrate()
    return coverage_map


def test_ceiling_scales_with_the_suite_rather_than_being_declared() -> None:
    """The same shape of repository at two sizes must yield proportional ceilings."""
    small = calibrated([2, 3, 4, 5, 6])
    large = calibrated([count * 10 for count in (2, 3, 4, 5, 6)])

    assert large.statistics()["covering_ceiling"] > small.statistics()["covering_ceiling"]
    # A fixed ceiling of 12 would reject the large repository's whole pool.
    assert len(large.excision_candidates()) == len(small.excision_candidates())


def test_a_pool_with_no_infrastructure_loses_nothing() -> None:
    """A percentile always cuts its top slice; a share only cuts what is broad.

    Forty evenly-exercised symbols over a suite none of them dominates: there is
    no infrastructure here, so the ceiling must reject none of them.
    """
    coverage_map = calibrated([4, 6, 8, 10] * 10)

    assert len(coverage_map.excision_candidates()) == 40


def test_the_widest_symbols_rank_last_rather_than_vanishing() -> None:
    """glom's real shape: two symbols carry most of the suite, the rest do not."""
    coverage_map = calibrated([3, 3, 6, 10, 10, 12, 14, 21, 28, 100, 119])
    order = [len(s.covering_tests) for s in coverage_map.excision_candidates()]

    assert set(order[-2:]) == {100, 119}, "infrastructure must sort to the bottom"
    assert {3, 6, 10, 12, 14, 21, 28} <= set(order)
    # The preferred band is what the old filter would have returned.
    preferred = {len(s.covering_tests) for s in coverage_map.preferred_candidates()}
    assert 100 not in preferred and 119 not in preferred


def test_calibration_survives_a_round_trip(tmp_path: Path) -> None:
    """Only covered symbols are persisted; the ceiling must still reproduce."""
    from stress_stack.atomic import atomic_write_json

    original = calibrated([1] * 50 + [3, 9, 14, 21, 40])
    path = tmp_path / "coverage_map.json"
    atomic_write_json(path, original.to_dict())

    reloaded = cm.load(path)
    assert reloaded.statistics()["covering_ceiling"] == original.statistics()["covering_ceiling"]
    assert {s.symbol_id for s in reloaded.excision_candidates()} == {
        s.symbol_id for s in original.excision_candidates()
    }


def test_a_map_with_no_coverage_at_all_falls_back() -> None:
    coverage_map = cm.CoverageMap(status="available")
    assert coverage_map.calibrate() == cm._FALLBACK_CEILING
    assert coverage_map.excision_candidates() == []


def test_tests_index_inverts_the_mapping(tmp_path: Path) -> None:
    repository(tmp_path)
    graph = build_graph(tmp_path)
    result = cm.build(
        graph,
        {"pkg/mod.py": {8: ["t.test_a.test_run"], 20: ["t.test_a.test_run", "t.test_b.test_h"]}},
    )
    index = result.tests_index()

    assert "t.test_a::test_run" in index
    assert len(index["t.test_a::test_run"]) == 2
    assert index["t.test_b::test_h"] == ["pkg/mod.py::pkg.mod.helper"]


def test_measurements_outside_the_repository_are_dropped(tmp_path: Path) -> None:
    """The installed copy in site-packages is not the tree under analysis."""
    inside = tmp_path / "pkg" / "core.py"
    raw = {
        str(inside): {"3": ["tests/test_core.py::test_a"]},
        "/usr/lib/python3.12/site-packages/pkg/core.py": {"3": ["tests/test_core.py::test_a"]},
    }

    lines, status, reason = cm.relativize(raw, tmp_path)

    assert status == "available"
    assert reason is None
    assert lines == {"pkg/core.py": {3: ["tests/test_core.py::test_a"]}}


def test_coverage_measured_entirely_outside_the_repository_is_unavailable(
    tmp_path: Path,
) -> None:
    """An empty map reported as available is worse than an honest failure.

    A src-layout repository whose PYTHONPATH left the installed copy winning
    produced exactly this: every measured path outside the root, everything
    silently dropped, and a successful-looking coverage stage that gave excision
    mining nothing to rank.
    """
    raw = {
        "/usr/lib/python3.12/site-packages/pkg/core.py": {"3": ["t::a"]},
        "/usr/lib/python3.12/site-packages/pkg/other.py": {"7": ["t::b"]},
    }

    lines, status, reason = cm.relativize(raw, tmp_path)

    assert lines == {}
    assert status == "unavailable"
    assert reason == "all_2_measured_paths_outside_repository"


def test_a_suite_that_measured_nothing_is_not_reported_as_misplaced(tmp_path: Path) -> None:
    """No data at all is a different failure from data about the wrong code."""
    assert cm.relativize({}, tmp_path) == ({}, "available", None)
