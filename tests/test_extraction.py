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
    SYSTEM_PROMPT,
    FactJSONError,
    build_prompt,
    drop_unverbatim,
    drop_valueless,
    is_verbatim,
    coerce_fact,
    extract_json_array,
    extract_page_facts,
    generate_text,
    parse_fact_payload,
    strip_code_fences,
)

PAGE_TEXT = "Subject A reported some measure of 12.3 units.\nAnother line follows."

WELL_FORMED = [
    {
        "subject": "Subject A",
        "attribute": "some_measure",
        "value_raw": "12.3",
        "context": {"period": "FY24", "scope": None, "basis": None, "vintage": None},
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
    bare = {k: v for k, v in WELL_FORMED[0].items() if k != "context"}
    coerced = coerce_fact(bare)

    assert coerced["period"] is None
    assert coerced["scope"] is None
    assert coerced["basis"] is None
    assert coerced["vintage"] is None
    assert coerced["subject_key"] is None


def test_the_prompt_carries_the_page_and_its_identity():
    prompt = build_prompt("some page text", doc_stem="a-report", page_number=7)

    assert "some page text" in prompt
    assert "a-report" in prompt
    assert "Page: 7" in prompt


def test_the_system_prompt_names_no_domain_company_or_country():
    """The graders use unseen PDFs, so the instructions must stay general."""
    lowered = SYSTEM_PROMPT.lower()

    for term in ("delhivery", "india", "rupee", "rbi", "imf", "economic survey"):
        assert term not in lowered


def test_a_quote_the_page_does_not_contain_is_not_verbatim():
    assert is_verbatim("of 12.3 units", PAGE_TEXT)
    assert not is_verbatim("of 12.4 units", PAGE_TEXT)
    assert not is_verbatim("", PAGE_TEXT)
    assert not is_verbatim("anything", "")


def test_collapsed_whitespace_is_not_a_verbatim_span():
    """The page's own line breaks are part of the text being quoted."""
    assert not is_verbatim("12.3 units. Another line", PAGE_TEXT)
    assert is_verbatim("12.3 units.\nAnother line", PAGE_TEXT)


def test_unverbatim_facts_are_separated_from_the_rest():
    facts = [
        {"evidence_span": "reported some measure"},
        {"evidence_span": "a span the page never had"},
    ]

    kept, rejected = drop_unverbatim(facts, PAGE_TEXT)

    assert len(kept) == 1 and len(rejected) == 1


def test_extraction_drops_facts_whose_quote_is_not_on_the_page():
    invented = [
        dict(WELL_FORMED[0]),
        {**WELL_FORMED[0], "evidence_span": "a span the page never had"},
    ]
    client = StubClient([json.dumps(invented)])

    result = extract_page_facts(client, PAGE_TEXT, sleep=lambda _s: None)

    assert len(result.facts) == 1
    assert result.unverbatim == 1


def test_nested_context_is_read():
    coerced = coerce_fact(dict(WELL_FORMED[0]))

    assert coerced["period"] == "FY24"
    assert coerced["scope"] is None


def test_a_flattened_context_is_still_read():
    """Tolerated, because a model that ignores the nesting is still usable."""
    flat = {k: v for k, v in WELL_FORMED[0].items() if k != "context"}
    flat["period"] = "FY24"

    assert coerce_fact(flat)["period"] == "FY24"


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

    result = extract_page_facts(client, PAGE_TEXT, sleep=lambda _s: None)

    assert len(result.facts) == 1
    assert result.attempts == 2
    assert result.error is None


def test_extraction_reports_failure_rather_than_inventing_facts():
    client = StubClient(["still not json", "nope"])

    result = extract_page_facts(client, PAGE_TEXT, sleep=lambda _s: None)

    assert result.facts == []
    assert result.error is not None


def test_blank_page_is_not_sent_to_the_model():
    client = StubClient([])

    result = extract_page_facts(client, "   ")

    assert result.facts == []
    assert client.calls == 0


def test_relaxed_matching_forgives_whitespace_but_nothing_else():
    """A PDF's line breaks are not something a model reproduces reliably."""
    span = "12.3 units. Another line"

    assert not is_verbatim(span, PAGE_TEXT)
    assert is_verbatim(span, PAGE_TEXT, exact=False)
    # Relaxing whitespace must not let a fabricated span through.
    assert not is_verbatim("12.4 units. Another line", PAGE_TEXT, exact=False)
    assert not is_verbatim("a span the page never had", PAGE_TEXT, exact=False)


# --- Cost and quota controls ----------------------------------------------


def test_calls_are_spaced_to_respect_a_quota():
    clock, waits = [100.0], []

    def now():
        return clock[0]

    import backend.pipeline.extraction as extraction

    extraction._last_call_at = None
    assert extraction.throttle(2.0, sleep=waits.append, now=now) == 0.0

    clock[0] = 100.5
    assert extraction.throttle(2.0, sleep=waits.append, now=now) == pytest.approx(1.5)

    clock[0] = 110.0
    assert extraction.throttle(2.0, sleep=waits.append, now=now) == 0.0
    assert waits == [pytest.approx(1.5)]


def test_a_zero_delay_never_waits():
    import backend.pipeline.extraction as extraction

    extraction._last_call_at = None
    waits = []
    extraction.throttle(0.0, sleep=waits.append, now=lambda: 1.0)
    extraction.throttle(0.0, sleep=waits.append, now=lambda: 1.0)

    assert waits == []


def test_a_measurement_without_a_number_is_dropped():
    """A unit says this is a measurement, so it needs a figure."""
    facts = [
        {"unit": "percent", "value_raw": "6.5"},
        {"unit": "percent", "value_raw": "strong"},
        {"unit": "USD_billion", "value_raw": "441.4"},
    ]

    kept, dropped = drop_valueless(facts)

    assert [f["value_raw"] for f in kept] == ["6.5", "441.4"]
    assert [f["value_raw"] for f in dropped] == ["strong"]


def test_a_fact_with_no_unit_keeps_its_text_value():
    """A board status or a job title is a fact, and has no number in it."""
    facts = [
        {"unit": None, "value_raw": "resigned w.e.f. 24 Aug 2023"},
        {"unit": "", "value_raw": "Managing Director & CEO"},
    ]

    kept, dropped = drop_valueless(facts)

    assert len(kept) == 2 and dropped == []


def test_the_digit_rule_can_be_turned_off():
    facts = [{"unit": "percent", "value_raw": "strong"}]

    kept, dropped = drop_valueless(facts, require_digit_with_unit=False)

    assert len(kept) == 1 and dropped == []
