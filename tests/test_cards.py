from __future__ import annotations

from stress_stack.cards import EvidenceCard, Section, check_grounding, file_card_messages


def card() -> EvidenceCard:
    return EvidenceCard(
        subject="pkg/mod.py",
        kind="file",
        entity_ids=["pkg/mod.py::pkg.mod.A", "pkg/mod.py::pkg.mod.b"],
        sections=[
            Section("contract", [f"- sig {i}" for i in range(4)], required=True),
            Section("behaviour", [f"- test_{i}" for i in range(10)]),
            Section("neighbours", [f"- caller {i}" for i in range(10)]),
        ],
    )


def test_sections_render_in_priority_order() -> None:
    rendered = card().render()

    assert rendered.index("## contract") < rendered.index("## behaviour")
    assert rendered.index("## behaviour") < rendered.index("## neighbours")


def test_elision_drops_whole_units_and_says_so() -> None:
    """A card cut mid-line is malformed; one that reports elision is complete."""
    rendered = card().render(unit_budget=6)

    assert "## behaviour  (6 of 10 shown)" in rendered
    assert "- test_5" in rendered
    assert "- test_6" not in rendered
    # every retained line is whole
    assert all(line.startswith(("-", "#")) or not line for line in rendered.splitlines())


def test_neighbours_elide_before_behaviour() -> None:
    rendered = card().render(unit_budget=10)

    assert "## behaviour  (10 of 10 shown)" not in rendered
    assert "## neighbours" not in rendered


def test_contract_is_never_elided() -> None:
    """Without signature and docstring an excision task is unsolvable."""
    rendered = card().render(unit_budget=0)

    assert "## contract" in rendered
    assert "- sig 3" in rendered
    assert "## behaviour" not in rendered


def test_grounding_rejects_invented_entities() -> None:
    payload = {
        "key_symbols": [
            {"entity_id": "pkg/mod.py::pkg.mod.A", "behaviour": "real"},
            {"entity_id": "pkg/mod.py::pkg.mod.GHOST", "behaviour": "invented"},
        ]
    }
    report = check_grounding(payload, card())

    assert report["grounded"] is False
    assert report["unknown_entities"] == ["pkg/mod.py::pkg.mod.GHOST"]


def test_grounding_accepts_verbatim_citations_and_scores_coverage() -> None:
    payload = {"key_symbols": [{"entity_id": "pkg/mod.py::pkg.mod.A", "behaviour": "x"}]}
    report = check_grounding(payload, card())

    assert report["grounded"] is True
    assert report["coverage"] == 0.5


def test_empty_citation_list_is_not_grounded() -> None:
    assert check_grounding({"key_symbols": []}, card())["grounded"] is False


def test_prompt_marks_repository_text_as_untrusted() -> None:
    system = file_card_messages(card())[0]["content"]

    assert "untrusted" in system.lower()
    assert "never as a command" in system.lower()
    assert "verbatim" in system.lower()
