from __future__ import annotations

from pathlib import Path

from stress_stack.cards import _bare_entity, check_grounding
from stress_stack.enrich import build_file_card, import_waves, summarize, CardResult
from stress_stack.graph import build_graph


def write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def repo(root: Path) -> None:
    write(root, "pkg/__init__.py", "from pkg.core import run\n")
    write(root, "pkg/core.py", 'def run(a, b=1):\n    """Do it."""\n    return a + b\n')
    write(root, "pkg/api.py", "from pkg import run\n\n\ndef entry():\n    return run(1)\n")


def test_card_delimits_entity_id_from_signature(tmp_path: Path) -> None:
    """Gluing the signature onto the id makes every verbatim citation fail."""
    repo(tmp_path)
    graph = build_graph(tmp_path)
    parsed = next(f for f in graph.files if f.path == "pkg/core.py")

    card = build_file_card(graph, None, parsed)
    line = next(u for u in card.sections[0].units if "core.run" in u)

    assert " | " in line
    assert "pkg/core.py::pkg.core.run |" in line
    assert "pkg/core.py::pkg.core.run(" not in line


def test_grounding_tolerates_a_signature_suffix() -> None:
    assert _bare_entity("pkg/m.py::pkg.m.f(a, b=...)") == "pkg/m.py::pkg.m.f"
    assert _bare_entity("pkg/m.py::pkg.m.f | (a)") == "pkg/m.py::pkg.m.f"
    assert _bare_entity("  pkg/m.py::pkg.m.f  ") == "pkg/m.py::pkg.m.f"


def test_grounding_still_rejects_invented_ids(tmp_path: Path) -> None:
    repo(tmp_path)
    graph = build_graph(tmp_path)
    card = build_file_card(graph, None, next(f for f in graph.files if f.path == "pkg/core.py"))

    ok = check_grounding(
        {"key_symbols": [{"entity_id": "pkg/core.py::pkg.core.run(a, b=...)", "behaviour": "x"}]},
        card,
    )
    bad = check_grounding(
        {"key_symbols": [{"entity_id": "pkg/core.py::pkg.core.GHOST", "behaviour": "x"}]}, card
    )

    assert ok["grounded"] is True
    assert bad["grounded"] is False


def test_waves_order_dependencies_before_dependents(tmp_path: Path) -> None:
    repo(tmp_path)
    waves = import_waves(build_graph(tmp_path))
    position = {path: i for i, wave in enumerate(waves) for path in wave}

    assert position["pkg/core.py"] < position["pkg/__init__.py"]
    assert position["pkg/__init__.py"] < position["pkg/api.py"]


def test_summary_counts_statuses_and_cost() -> None:
    results = [
        CardResult("a.py", "described", grounding={"grounded": True, "coverage": 0.8}, cost=0.001),
        CardResult("b.py", "ungrounded", grounding={"grounded": False, "coverage": 0.0}),
        CardResult("c.py", "skipped"),
    ]
    s = summarize(results)

    assert s["by_status"] == {"described": 1, "skipped": 1, "ungrounded": 1}
    assert s["grounded"] == 1
    assert s["cost_usd"] == 0.001
