from __future__ import annotations

from collections.abc import Callable

import pytest

from oxison.prompts import (
    manual_prompt,
    product_prompt,
    single_pass_prompt,
    slice_prompt,
    stack_prompt,
    synthesis_prompt,
)


def test_single_pass_prompt_without_extra_is_unchanged() -> None:
    p = single_pass_prompt(root="/r", repo_map_context="MAP")
    assert "ADDITIONAL SOURCES" not in p
    assert "MAP" in p


def test_single_pass_prompt_injects_extra_context() -> None:
    p = single_pass_prompt(root="/r", repo_map_context="MAP", extra_context="EXTRA-BLOCK")
    assert "EXTRA-BLOCK" in p
    assert p.index("MAP") < p.index("EXTRA-BLOCK")  # extra comes after the map


def test_product_prompt_injects_extra_context() -> None:
    p = product_prompt(
        root="/r",
        comprehension="C",
        repo_map_context="MAP",
        extra_context="EXTRA-BLOCK",
    )
    assert "EXTRA-BLOCK" in p


def test_all_six_builders_accept_extra_context() -> None:
    # smoke: every builder accepts the kwarg and includes the block
    assert "EB" in single_pass_prompt(root="/r", repo_map_context="M", extra_context="EB")
    assert "EB" in slice_prompt(
        root="/r", repo_map_context="M", slice_dir="d", extra_context="EB"
    )
    assert "EB" in synthesis_prompt(
        root="/r", repo_map_context="M", slice_summaries="s", extra_context="EB"
    )
    assert "EB" in product_prompt(
        root="/r", comprehension="C", repo_map_context="M", extra_context="EB"
    )
    assert "EB" in manual_prompt(
        root="/r", comprehension="C", repo_map_context="M", extra_context="EB"
    )
    assert "EB" in stack_prompt(
        root="/r", comprehension="C", repo_map_context="M", extra_context="EB"
    )


@pytest.mark.parametrize(
    "call",
    [
        lambda ec: single_pass_prompt(root="/r", repo_map_context="MAP", extra_context=ec),
        lambda ec: slice_prompt(
            root="/r", repo_map_context="MAP", slice_dir="d", extra_context=ec
        ),
        lambda ec: synthesis_prompt(
            root="/r", repo_map_context="MAP", slice_summaries="s", extra_context=ec
        ),
        lambda ec: product_prompt(
            root="/r", comprehension="C", repo_map_context="MAP", extra_context=ec
        ),
        lambda ec: manual_prompt(
            root="/r", comprehension="C", repo_map_context="MAP", extra_context=ec
        ),
        lambda ec: stack_prompt(
            root="/r", comprehension="C", repo_map_context="MAP", extra_context=ec
        ),
    ],
)
def test_empty_extra_context_is_byte_identical_across_all_builders(
    call: Callable[[str], str],
) -> None:
    # extra_context="" must produce byte-identical output to a whitespace-only
    # value AND to the no-injection baseline (the _extra_block strip guard).
    assert call("") == call("   \n  ")
    assert "ADDITIONAL SOURCES" not in call("")
    assert "INJECTED" in call("INJECTED")  # non-empty still injects
