"""Tests for reading and retrying model responses.

These exercise the parsing and retry machinery, not the content of any
document: the payloads below are deliberately synthetic shapes used to check
that malformed output is handled, and no value here is treated as a fact about
anything. Facts that reach the database come only from real PDFs.
"""

import json

import pytest
from google.genai import errors

from backend.pipeline.extraction import (
    FactJSONError,
    build_prompt,
    coerce_fact,
    extract_json_array,
    extract_page_facts,
    generate_text,
    parse_fact_payload,
    strip_code_fences,
)

WELL_FORMED = [
    {
        "subject": "Subject A",
        "attribute": "some_measure",
        "value_raw": "12.3",
        "evidence_span": "Subject A reported some measure of 12.3 units.",
        "confidence": 0.8,
    }
]


class StubClient:
    """Stands in for the Gemini client, returning queued replies in order."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.models = self

    def generate_content(self, **kwargs):
        self.calls += 1
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return type("Response", (), {"text": reply})()


def test_strip_code_fences_removes_json_wrapper():
    assert strip_code_fences('```json\n[{"a": 1}]\n```') == '[{"a": 1}]'
    assert strip_code_fences("```\n[]\n```") == "[]"
    assert strip_code_fences("[]") == "[]"


def test_extract_json_array_ignores_brackets_inside_strings():
    text = 'Here you go: [{"evidence_span": "a ] bracket [ inside"}] hope that helps'

    array = extract_json_array(text)

    assert json.loads(array)[0]["evidence_span"] == "a ] bracket [ inside"


def test_extract_json_array_returns_none_when_absent():
    assert extract_json_array("no array here") is None


def test_parse_accepts_a_plain_array():
    assert len(parse_fact_payload(json.dumps(WELL_FORMED))) == 1


def test_parse_accepts_fenced_and_prose_wrapped_output():
    body = json.dumps(WELL_FORMED)

    assert len(parse_fact_payload(f"```json\n{body}\n```")) == 1
    assert len(parse_fact_payload(f"Sure! Here are the facts:\n{body}\nLet me know.")) == 1


def test_parse_accepts_a_single_object_or_wrapper_key():
    assert len(parse_fact_payload(json.dumps(WELL_FORMED[0]))) == 1
    assert len(parse_fact_payload(json.dumps({"facts": WELL_FORMED}))) == 1


def test_parse_rejects_unusable_responses():
    with pytest.raises(FactJSONError):
        parse_fact_payload("")
    with pytest.raises(FactJSONError):
        parse_fact_payload("I could not find any facts.")
    with pytest.raises(FactJSONError):
        parse_fact_payload('[{"subject": "A", ')  # truncated mid-array


def test_facts_missing_required_fields_are_dropped():
    payload = [
        dict(WELL_FORMED[0]),
        {"subject": "B", "attribute": "x", "value_raw": "1"},  # no evidence_span
        {"subject": "C", "attribute": "x", "evidence_span": "q"},  # no value
        {"attribute": "x", "value_raw": "1", "evidence_span": "q"},  # no subject
        "not an object",
    ]

    assert len(parse_fact_payload(json.dumps(payload))) == 1


def test_coerce_normalises_numbers_and_clamps_confidence():
    coerced = coerce_fact(
        {
            "subject": " A ",
            "attribute": "m",
            "value_raw": 12.5,
            "evidence_span": "q",
            "confidence": 4.2,
        }
    )

    assert coerced["subject"] == "A"
    assert coerced["value_raw"] == "12.5"
    assert coerced["confidence"] == 1.0
    assert coerce_fact({"confidence": "bad"}) is None


def test_context_fields_default_to_none_rather_than_being_guessed():
    coerced = coerce_fact(dict(WELL_FORMED[0]))

    assert coerced["period"] is None
    assert coerced["scope"] is None
    assert coerced["basis"] is None
    assert coerced["vintage"] is None
    assert coerced["subject_key"] is None


def test_prompt_includes_tables_only_when_present():
    plain = build_prompt("page text")
    with_tables = build_prompt("page text", [[["h1", "h2"], ["a", None]]])

    assert "TABLES DETECTED" not in plain
    assert "TABLES DETECTED" in with_tables
    assert "h1 | h2" in with_tables


def test_generate_retries_rate_limits_then_succeeds():
    client = StubClient([errors.ClientError(429, {"error": {"message": "slow down"}}), "[]"])
    slept = []

    result = generate_text(client, "prompt", sleep=slept.append)

    assert result == "[]"
    assert client.calls == 2
    assert len(slept) == 1 and slept[0] > 0


def test_generate_retries_transient_server_errors():
    client = StubClient([errors.ServerError(503, {"error": {"message": "busy"}}), "[]"])

    assert generate_text(client, "prompt", sleep=lambda _s: None) == "[]"


def test_generate_does_not_retry_a_bad_request():
    client = StubClient([errors.ClientError(400, {"error": {"message": "bad"}})])

    with pytest.raises(errors.ClientError):
        generate_text(client, "prompt", sleep=lambda _s: None)
    assert client.calls == 1


def test_generate_gives_up_after_max_attempts():
    rate_limited = [
        errors.ClientError(429, {"error": {"message": "slow down"}}) for _ in range(3)
    ]
    client = StubClient(rate_limited)

    with pytest.raises(errors.ClientError):
        generate_text(client, "prompt", max_attempts=3, sleep=lambda _s: None)
    assert client.calls == 3


def test_extraction_retries_once_on_unparseable_json():
    client = StubClient(["not json at all", json.dumps(WELL_FORMED)])

    result = extract_page_facts(client, "page text", sleep=lambda _s: None)

    assert len(result.facts) == 1
    assert result.attempts == 2
    assert result.error is None


def test_extraction_reports_failure_rather_than_inventing_facts():
    client = StubClient(["still not json", "nope"])

    result = extract_page_facts(client, "page text", sleep=lambda _s: None)

    assert result.facts == []
    assert result.error is not None


def test_blank_page_is_not_sent_to_the_model():
    client = StubClient([])

    result = extract_page_facts(client, "   ")

    assert result.facts == []
    assert client.calls == 0
