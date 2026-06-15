"""Tests for the model-output JSON extractor."""

from __future__ import annotations

import pytest

from oxison.jsonutil import JsonExtractError, extract_json_object


def test_plain_object():
    assert extract_json_object('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}


def test_json_code_fence():
    text = '```json\n{"a": 1}\n```'
    assert extract_json_object(text) == {"a": 1}


def test_bare_code_fence():
    text = '```\n{"a": 1}\n```'
    assert extract_json_object(text) == {"a": 1}


def test_preamble_before_object():
    text = 'Here is the roadmap you asked for:\n{"tasks": []}'
    assert extract_json_object(text) == {"tasks": []}


def test_trailing_text_after_object():
    text = '{"a": 1}\n\nLet me know if you want changes!'
    assert extract_json_object(text) == {"a": 1}


def test_nested_braces_and_strings_with_braces():
    text = '{"summary": "use {curly} braces", "tasks": [{"title": "x"}]}'
    out = extract_json_object(text)
    assert out["summary"] == "use {curly} braces"
    assert out["tasks"][0]["title"] == "x"


def test_escaped_quote_inside_string():
    text = '{"q": "she said \\"hi\\" loudly"}'
    assert extract_json_object(text) == {"q": 'she said "hi" loudly'}


def test_array_wrapped_returns_first_object():
    # The model contract is a single object; if it returns an array we extract
    # the first balanced object inside it rather than failing outright.
    assert extract_json_object('[{"a": 1}, {"b": 2}]') == {"a": 1}


def test_brace_containing_preamble():
    # The planner prompt models {root}/{id} placeholders; the model may echo a
    # brace before the real object. The extractor must skip the junk brace.
    text = 'I reference {root}/src and {id} above. Plan:\n{"summary": "s", "tasks": []}'
    assert extract_json_object(text) == {"summary": "s", "tasks": []}


def test_skips_invalid_object_then_finds_valid():
    text = '{not valid json} ... but then {"a": 1}'
    assert extract_json_object(text) == {"a": 1}


def test_empty_raises():
    with pytest.raises(JsonExtractError):
        extract_json_object("   ")


def test_no_object_raises():
    with pytest.raises(JsonExtractError):
        extract_json_object("there is no json here at all")
