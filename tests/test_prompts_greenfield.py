"""Greenfield prompt builders + planner greenfield framing."""
from __future__ import annotations

from oxison.prompts import (
    GREENFIELD_IDENTITY,
    greenfield_comprehension_prompt,
    greenfield_product_prompt,
    roadmap_plan_prompt,
)


def test_identity_says_no_codebase() -> None:
    low = GREENFIELD_IDENTITY.lower()
    assert "no existing codebase" in low
    assert "do not attempt to read files" in low


def test_greenfield_comprehension_omits_repo_map() -> None:
    p = greenfield_comprehension_prompt(extra_context="=== ADDITIONAL SOURCES ===\nx")
    assert "=== REPOSITORY MAP ===" not in p
    assert "ADDITIONAL SOURCES" in p
    assert "PROPOSED product" in p


def test_greenfield_comprehension_empty_extra_is_fine() -> None:
    p = greenfield_comprehension_prompt(extra_context="")
    assert isinstance(p, str) and p.strip()
    assert "ADDITIONAL SOURCES" not in p  # _extra_block empty → nothing injected


def test_greenfield_product_frames_as_to_build() -> None:
    p = greenfield_product_prompt(comprehension="# Vision")
    assert "to be built" in p or "to BUILD" in p
    assert "=== COMPREHENSION ===" in p


def test_planner_greenfield_toggle() -> None:
    common = {
        "product_name": "App",
        "comprehension_markdown": "# App",
        "structured_state": "{}",
        "open_questions": "(none)",
    }
    on = roadmap_plan_prompt(**common, greenfield=True)
    off = roadmap_plan_prompt(**common, greenfield=False)
    assert "GREENFIELD BUILD" in on
    assert "GREENFIELD BUILD" not in off


def test_planner_default_matches_greenfield_false() -> None:
    common = {
        "product_name": "App",
        "comprehension_markdown": "# App",
        "structured_state": "{}",
        "open_questions": "(none)",
    }
    # Default (no kwarg) must be byte-identical to greenfield=False (no regression).
    assert roadmap_plan_prompt(**common) == roadmap_plan_prompt(**common, greenfield=False)
