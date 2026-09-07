"""Fact extraction from a page of text, using Gemini with structured output.

The model's only jobs are to propose a fact and to copy the span of text that
supports it. It is explicitly told not to compute, convert or locate anything:
values are copied as written, and the page a quote sits on is decided later by
grounding, not here.

Everything that parses a model response is a pure function, so the awkward
cases - code fences, prose wrapped around the JSON, a truncated array - are
testable without touching the network.
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass

from pydantic import BaseModel

from backend import config

MAX_ATTEMPTS = 4
BASE_BACKOFF_SECONDS = 1.0

# When the last model call finished, so calls can be spaced out. A free-tier
# quota is per minute, and going over it costs far more time in backoff than
# waiting politely does.
_last_call_at: float | None = None

# Retried: rate limiting and transient server faults.
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class FactJSONError(ValueError):
    """The model's response could not be read as a list of facts."""


class ExtractedContext(BaseModel):
    """The conditions a value holds under, as the page states them."""

    period: str | None = None
    scope: str | None = None
    basis: str | None = None
    vintage: str | None = None


class ExtractedFact(BaseModel):
    """What the model returns per fact. Grounding fields are absent by design."""

    subject: str
    attribute: str
    value_raw: str
    unit: str | None = None
    context: ExtractedContext = ExtractedContext()
    evidence_span: str
    confidence: float = 0.5


SYSTEM_PROMPT = """\
You extract structured, grounded facts from ONE page of a document. The document may be
a financial filing, an economic report, a corporate disclosure, or anything else — never
assume a domain, a company, or a country. Work only from the page text you are given.

Return ONLY a JSON array of fact objects. No prose, no markdown, no code fences. If the
page has no extractable facts, return [].

Extract a fact only if it is EXPLICITLY stated on this page. Never infer from outside
knowledge, never compute values that aren't printed, never carry context from a page you
cannot see. If you are unsure whether something is on the page, do not emit it.

Each fact object has exactly these fields:

- "subject": the entity the fact is about, as named on the page (a company, country,
  person, segment, etc.). Use the most specific named entity present. If the page refers
  to "the Company" / "your Company" and the specific name is not on THIS page, keep it as
  written — do not substitute a name from memory.
- "attribute": a concise snake_case key for WHAT is measured, e.g. "revenue_from_operations",
  "total_income", "real_gdp_growth", "cpi_inflation", "current_account_deficit",
  "forex_reserves", "board_status", "shipment_volume". Normalize the wording to a stable
  key. DO NOT put the period, scope, unit, or year inside the attribute.
- "value_raw": the value exactly as printed — "81,415.38", "6.4 per cent", "USD 640.3
  billion", "resigned", "740". Copy it verbatim.
- "unit": the unit if indicated, using a stable token: "INR_million", "INR_crore",
  "INR_lakh", "USD_billion", "percent", "count_million", etc. If no unit is indicated,
  use null. NEVER guess a unit that isn't shown.
- "context": an object with these keys, each null unless stated or unambiguous on the page:
    - "period": the time the value refers to, as written — "FY24", "Q4 FY24", "H1 FY25",
      "2024-25", "FY2024/25", "April–December 2024", "as of December 2024". null if none.
    - "scope": "consolidated" or "standalone" if indicated, else null.
    - "basis": a measurement-basis token if indicated — "real", "nominal",
      "GDP_market_price", "GVA", "restated", else null.
    - "vintage": "advance_estimate", "provisional", "revised", "final", or "projection"
      if the page signals it (e.g. "estimated", "projected", "revised"), else null.
- "evidence_span": the EXACT substring of the page text that contains this value. Copy it
  character-for-character from the text provided — same digits, same punctuation, same
  spacing. Keep it as short as possible while still containing the value and enough words
  to identify what it is. This string MUST appear verbatim in the page text.
- "confidence": a number 0.0–1.0 for how sure you are the fact is correctly read from the
  page. Lower it for messy tables, ambiguous units, or unclear subjects.

Rules:
- Emit ONE object per distinct (subject, attribute, value, context). A table row that
  reports the same metric across several columns (e.g. standalone/consolidated, or several
  years) becomes SEVERAL facts — one per column — each tagged with the scope/period from
  its column or row header.
- If a value has no resolvable unit (a bare number), still extract it with "unit": null and
  a lower confidence — do not drop it, and do not invent a unit.
- Skip running headers, footers, page numbers, and narrative that states no concrete value.
- A value must be something the page states concretely: a quantity, a date, a
  status, a name, or an identifier. Do NOT emit a judgement of degree or quality
  as a value ("strong", "benign", "robust", "well contained", "above trend",
  "resilient", "subdued"). Where the page offers only an adjective in place of a
  figure, there is no fact to extract. Do NOT extract table-of-contents entries,
  chapter or section numbers, page references, or contact details.
- NEVER output a value that is not present in the page text. NEVER output an evidence_span
  that is not a verbatim substring of the page text.
"""

USER_TEMPLATE = '''Document: {doc_stem}   Page: {page_number}

PAGE TEXT:
"""
{page_text}
"""

Extract every stated fact from this page as a JSON array following the schema. Return only
the JSON array.
'''


@dataclass
class ExtractionResult:
    """Raw facts proposed for one page, before grounding."""

    page_index: int
    facts: list[dict]
    attempts: int = 1
    error: str | None = None
    # Facts the model proposed whose quote was not actually on the page.
    unverbatim: int = 0
    # Measurements proposed with no number in them.
    valueless: int = 0


def strip_code_fences(text: str) -> str:
    """Remove a ```json ... ``` wrapper if the model added one."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    lines = lines[1:]  # drop the opening fence and any language tag
    while lines and lines[-1].strip().startswith("```"):
        lines.pop()
    return "\n".join(lines).strip()


def _extract_bracketed(text: str, opener: str, closer: str) -> str | None:
    """Pull the outermost bracketed block out of surrounding prose.

    Bracket counting is string- and escape-aware, so a bracket inside a quoted
    evidence span does not end the block early.
    """
    start = text.find(opener)
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False

    for position in range(start, len(text)):
        char = text[position]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start : position + 1]

    return None


def extract_json_array(text: str) -> str | None:
    """The outermost JSON array in a response."""
    return _extract_bracketed(text, "[", "]")


def extract_json_object(text: str) -> str | None:
    """The outermost JSON object in a response."""
    return _extract_bracketed(text, "{", "}")


def coerce_fact(raw: object) -> dict | None:
    """Keep a proposed fact only if it carries the fields a fact needs.

    A fact with no value or no quote cannot be grounded or compared, so it is
    dropped rather than stored in a half-usable state.
    """
    if not isinstance(raw, dict):
        return None

    def text_field(name: str) -> str | None:
        value = raw.get(name)
        if value is None:
            return None
        if isinstance(value, (int, float)):
            value = str(value)
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        return cleaned or None

    subject = text_field("subject")
    attribute = text_field("attribute")
    value_raw = text_field("value_raw")
    evidence_span = text_field("evidence_span")

    if not (subject and attribute and value_raw and evidence_span):
        return None

    try:
        confidence = float(raw.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5

    # The schema nests the context; a model that flattens it is still read.
    nested = raw.get("context")
    context_source = nested if isinstance(nested, dict) else raw

    def context_field(name: str) -> str | None:
        value = context_source.get(name)
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        return cleaned or None

    return {
        "subject": subject,
        "subject_key": text_field("subject_key"),
        "attribute": attribute,
        "value_raw": value_raw,
        "unit": text_field("unit"),
        "period": context_field("period"),
        "scope": context_field("scope"),
        "basis": context_field("basis"),
        "vintage": context_field("vintage"),
        "evidence_span": evidence_span,
        "confidence": min(max(confidence, 0.0), 1.0),
    }


def parse_fact_payload(text: str) -> list[dict]:
    """Read a model response into a list of usable fact dicts."""
    if not text or not text.strip():
        raise FactJSONError("empty response")

    candidate = strip_code_fences(text)

    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        array = extract_json_array(candidate)
        if array is None:
            raise FactJSONError("no JSON array found in response") from None
        try:
            payload = json.loads(array)
        except json.JSONDecodeError as exc:
            raise FactJSONError(f"malformed JSON array: {exc}") from exc

    # A single object, or an array wrapped in a key, are both common slips.
    if isinstance(payload, dict):
        for key in ("facts", "results", "data", "items"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            payload = [payload]

    if not isinstance(payload, list):
        raise FactJSONError(f"expected a list of facts, got {type(payload).__name__}")

    return [fact for fact in (coerce_fact(item) for item in payload) if fact]


def build_prompt(page_text: str, doc_stem: str = "", page_number: int | str = "") -> str:
    """The user message for one page."""
    return USER_TEMPLATE.format(
        doc_stem=doc_stem, page_number=page_number, page_text=page_text
    )


def is_verbatim(evidence_span: str, page_text: str, *, exact: bool = True) -> bool:
    """Whether a quote really appears on the page.

    A span the page does not contain cannot be evidence of anything, so a fact
    carrying one is discarded rather than trusted. This is what holds the model
    to the prompt's demand for a copied span.

    ``exact`` requires a character-for-character substring. Relaxing it forgives
    only whitespace: the words and digits must still all be present, in order,
    on that page. That distinction matters because a PDF's line breaks fall in
    places a model does not reliably reproduce, and a run of table figures is
    the usual casualty.
    """
    if not evidence_span or not page_text:
        return False
    if evidence_span in page_text:
        return True
    if exact:
        return False
    return " ".join(evidence_span.split()) in " ".join(page_text.split())


_DIGIT = re.compile(r"\d")


def is_numeric_claim(fact: dict) -> bool:
    """Whether a fact presents itself as a measurement.

    A stated unit is the signal. A fact without one may still be a real fact -
    a role, a status, a date - so it is not held to the same rule.
    """
    return bool((fact.get("unit") or "").strip())


def drop_valueless(
    facts: list[dict], *, require_digit_with_unit: bool = True
) -> tuple[list[dict], list[dict]]:
    """Discard measurements that carry no number.

    "6.5 percent" is a measurement; "strong percent" is not. Facts with no unit
    pass through untouched, so a board status or a job title survives.
    """
    if not require_digit_with_unit:
        return list(facts), []

    kept, rejected = [], []
    for fact in facts:
        numeric = is_numeric_claim(fact)
        ok = (not numeric) or bool(_DIGIT.search(fact.get("value_raw") or ""))
        (kept if ok else rejected).append(fact)
    return kept, rejected


def drop_unverbatim(
    facts: list[dict], page_text: str, *, exact: bool = True
) -> tuple[list[dict], list[dict]]:
    """Split proposed facts into those the page actually says, and those it does not."""
    kept, rejected = [], []
    for fact in facts:
        target = kept if is_verbatim(fact["evidence_span"], page_text, exact=exact) else rejected
        target.append(fact)
    return kept, rejected


def get_client(api_key: str | None = None):
    """Build a Gemini client. Raises if no key is configured."""
    from google import genai

    key = api_key or config.GEMINI_API_KEY
    if not key.strip():
        raise RuntimeError(
            "No GEMINI_API_KEY configured. Copy .env.example to .env and set it."
        )
    return genai.Client(api_key=key)


def _status_of(exc: Exception) -> int | None:
    return getattr(exc, "code", None) or getattr(exc, "status_code", None)


def throttle(delay: float | None = None, *, sleep=time.sleep, now=time.monotonic) -> float:
    """Wait until enough time has passed since the previous call.

    Returns how long it waited. Spacing requests is cheaper than being refused
    and backing off, and it keeps a long run inside a per-minute quota.
    """
    global _last_call_at

    gap = config.REQUEST_DELAY_SECONDS if delay is None else delay
    if gap <= 0:
        _last_call_at = now()
        return 0.0


    waited = 0.0
    if _last_call_at is not None:
        elapsed = now() - _last_call_at
        if elapsed < gap:
            waited = gap - elapsed
            sleep(waited)
    _last_call_at = now()
    return waited


def _sleep_for(attempt: int) -> float:
    """Exponential backoff with jitter, so retries do not sync up."""
    return BASE_BACKOFF_SECONDS * (2**attempt) * (0.5 + random.random())


def generate_text(
    client,
    prompt: str,
    *,
    model: str | None = None,
    response_schema=None,
    system_instruction: str | None = None,
    max_attempts: int = MAX_ATTEMPTS,
    sleep=time.sleep,
) -> str:
    """Call the model, retrying rate limits and transient server errors.

    ``response_schema`` shapes the structured output; it defaults to a list of
    facts, which is what extraction wants. Other callers pass their own.
    """
    from google.genai import errors, types

    thinking = (
        types.ThinkingConfig(thinking_budget=config.THINKING_BUDGET)
        if config.THINKING_BUDGET >= 0
        else None
    )
    config_kwargs = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=response_schema if response_schema is not None else list[ExtractedFact],
        system_instruction=system_instruction,
        thinking_config=thinking,
        temperature=0.0,
    )

    last_error: Exception | None = None
    for attempt in range(max_attempts):
        try:
            throttle(sleep=sleep)
            response = client.models.generate_content(
                model=model or config.GEMINI_EXTRACTION_MODEL,
                contents=prompt,
                config=config_kwargs,
            )
            return response.text or ""
        except errors.APIError as exc:
            status = _status_of(exc)
            if status not in RETRYABLE_STATUS or attempt == max_attempts - 1:
                raise
            last_error = exc
            sleep(_sleep_for(attempt))

    raise last_error if last_error else RuntimeError("generation failed")


def extract_page_facts(
    client,
    page_text: str,
    *,
    page_index: int = 0,
    doc_stem: str = "",
    page_number: int | None = None,
    model: str | None = None,
    exact_spans: bool | None = None,
    max_attempts: int = MAX_ATTEMPTS,
    sleep=time.sleep,
) -> ExtractionResult:
    """Extract facts from one page, retrying once if the JSON comes back broken.

    A fact whose evidence span is not a verbatim substring of the page is
    discarded here, before it can reach grounding. The prompt demands a
    character-for-character quote; this is what holds the model to it.
    """
    if not page_text or not page_text.strip():
        return ExtractionResult(page_index=page_index, facts=[])

    if exact_spans is None:
        exact_spans = config.REQUIRE_EXACT_SPANS

    prompt = build_prompt(
        page_text,
        doc_stem=doc_stem,
        page_number=page_number if page_number is not None else page_index + 1,
    )

    for attempt in range(2):
        try:
            raw = generate_text(
                client,
                prompt,
                model=model,
                system_instruction=SYSTEM_PROMPT,
                max_attempts=max_attempts,
                sleep=sleep,
            )
            proposed = parse_fact_payload(raw)
            grounded_spans, unverbatim = drop_unverbatim(
                proposed, page_text, exact=exact_spans
            )
            kept, valueless = drop_valueless(
                grounded_spans, require_digit_with_unit=config.REQUIRE_DIGIT_WITH_UNIT
            )
            return ExtractionResult(
                page_index=page_index,
                facts=kept,
                attempts=attempt + 1,
                unverbatim=len(unverbatim),
                valueless=len(valueless),
            )
        except FactJSONError as exc:
            if attempt == 0:
                # Ask again, naming the failure, before giving up on this page.
                prompt = (
                    f"{prompt}\n\nYour previous reply could not be parsed ({exc}). "
                    "Reply with a valid JSON array only, no prose and no code fences."
                )
                continue
            return ExtractionResult(
                page_index=page_index, facts=[], attempts=attempt + 1, error=str(exc)
            )

    return ExtractionResult(page_index=page_index, facts=[])
