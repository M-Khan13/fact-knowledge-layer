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
import time
from dataclasses import dataclass

from pydantic import BaseModel

from backend import config

MAX_ATTEMPTS = 4
BASE_BACKOFF_SECONDS = 1.0

# Retried: rate limiting and transient server faults.
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class FactJSONError(ValueError):
    """The model's response could not be read as a list of facts."""


class ExtractedFact(BaseModel):
    """What the model returns per fact. Grounding fields are absent by design."""

    subject: str
    subject_key: str | None = None
    attribute: str
    value_raw: str
    unit: str | None = None
    period: str | None = None
    scope: str | None = None
    basis: str | None = None
    vintage: str | None = None
    evidence_span: str
    confidence: float = 0.5


PROMPT = """\
You extract factual claims from a single page of a document.

Return a JSON array. Each element is one fact the page states explicitly.

Rules:
- Extract only what the page actually says. Never infer, calculate, convert,
  scale or combine values. If the page does not state it, it is not a fact.
- `value_raw` must be copied exactly as written, keeping digit separators,
  decimals and symbols. Do not normalise or convert it.
- `evidence_span` must be a VERBATIM substring of the PAGE TEXT below, copied
  character for character. It must be long enough to contain the value and to
  show what the value refers to. If you cannot copy an exact span from the page
  text, omit the fact entirely.
- `attribute` is a short snake_case name for what is being measured, chosen to
  fit this page's content. It is free text, not a fixed list. Prefer a specific
  name over a vague one.
- `subject` is the entity the fact is about, named as the page names it.
- `subject_key` is a strong, official identifier for that entity if the page
  states one. Otherwise null.
- `unit` is the unit exactly as written, if any. Otherwise null.
- `period`, `scope`, `basis` and `vintage` record the conditions under which
  the value holds, copied as the page expresses them. Use null where the page
  is silent. Never guess these.
    - `period`: the time the value covers.
    - `scope`: the reporting boundary the value is drawn over.
    - `basis`: which measure or definition the value uses.
    - `vintage`: whether the figure is an estimate, provisional, revised, final
      or a projection.
- `confidence` is 0 to 1: how certain you are the page states this fact.
- Do not report page numbers. Do not invent facts to fill the array.
- If the page states no extractable facts, return an empty array.

PAGE TEXT
---
{page_text}
---
{tables_block}"""

TABLES_TEMPLATE = """
TABLES DETECTED ON THIS PAGE (layout aid only; `evidence_span` must still be
copied from PAGE TEXT above, not from this block):
---
{tables}
---"""


@dataclass
class ExtractionResult:
    """Raw facts proposed for one page, before grounding."""

    page_index: int
    facts: list[dict]
    attempts: int = 1
    error: str | None = None


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

    return {
        "subject": subject,
        "subject_key": text_field("subject_key"),
        "attribute": attribute,
        "value_raw": value_raw,
        "unit": text_field("unit"),
        "period": text_field("period"),
        "scope": text_field("scope"),
        "basis": text_field("basis"),
        "vintage": text_field("vintage"),
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


def build_prompt(page_text: str, tables: list[list[list[str | None]]] | None = None) -> str:
    """Assemble the page prompt, including tables only when there are some."""
    tables_block = ""
    if tables:
        rendered = "\n\n".join(
            "\n".join(" | ".join((cell or "").strip() for cell in row) for row in table)
            for table in tables
        )
        tables_block = TABLES_TEMPLATE.format(tables=rendered)

    return PROMPT.format(page_text=page_text, tables_block=tables_block)


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


def _sleep_for(attempt: int) -> float:
    """Exponential backoff with jitter, so retries do not sync up."""
    return BASE_BACKOFF_SECONDS * (2**attempt) * (0.5 + random.random())


def generate_text(
    client,
    prompt: str,
    *,
    model: str | None = None,
    response_schema=None,
    max_attempts: int = MAX_ATTEMPTS,
    sleep=time.sleep,
) -> str:
    """Call the model, retrying rate limits and transient server errors.

    ``response_schema`` shapes the structured output; it defaults to a list of
    facts, which is what extraction wants. Other callers pass their own.
    """
    from google.genai import errors, types

    config_kwargs = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=response_schema if response_schema is not None else list[ExtractedFact],
        temperature=0.0,
    )

    last_error: Exception | None = None
    for attempt in range(max_attempts):
        try:
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
    tables: list[list[list[str | None]]] | None = None,
    model: str | None = None,
    max_attempts: int = MAX_ATTEMPTS,
    sleep=time.sleep,
) -> ExtractionResult:
    """Extract facts from one page, retrying once if the JSON comes back broken."""
    if not page_text or not page_text.strip():
        return ExtractionResult(page_index=page_index, facts=[])

    prompt = build_prompt(page_text, tables)

    for attempt in range(2):
        try:
            raw = generate_text(
                client, prompt, model=model, max_attempts=max_attempts, sleep=sleep
            )
            return ExtractionResult(
                page_index=page_index, facts=parse_fact_payload(raw), attempts=attempt + 1
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
