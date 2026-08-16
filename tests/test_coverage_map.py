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


def test_uncovered_symbols_are_recorded_but_excluded_from_output(tmp_path: Path) -> None:
    repository(tmp_path)
    graph = build_graph(tmp_path)
    result = cm.build(graph, {"pkg/mod.py": {8: ["t.test_a.test_run"]}})

    assert "pkg/mod.py::pkg.mod.helper" in result.symbols
    assert "pkg/mod.py::pkg.mod.helper" not in result.to_dict()["symbols"]


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
    assert covered(60).focus_score == 0.0


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


def test_the_widest_symbols_are_still_rejected() -> None:
    """glom's real shape: two symbols carry most of the suite, the rest do not."""
    coverage_map = calibrated([3, 3, 6, 10, 10, 12, 14, 21, 28, 100, 119])
    kept = {len(s.covering_tests) for s in coverage_map.excision_candidates()}

    assert 100 not in kept and 119 not in kept
    assert {3, 6, 10, 12, 14, 21, 28} <= kept


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
