"""The model's narrow role: explaining a verdict it did not choose.

Everything decided has already been decided. The adjudicator is handed a pair,
the verdict the rules reached and the reason code that proves it, and is asked
for one readable sentence. It may lower confidence where a case is genuinely
marginal; it may never raise it, and it may never change the verdict.

That constraint is enforced here rather than requested politely in a prompt: a
reply proposing a different verdict is discarded, and a confidence above the
deterministic one is clamped down. If the model is unavailable, every
relationship still carries the explanation the rules wrote for it, so nothing
depends on adjudication having run.
"""

from __future__ import annotations

import json
import time

from pydantic import BaseModel

from backend import config
from backend.pipeline.extraction import FactJSONError
from backend.pipeline.reconciliation import Verdict

MAX_REASON_LENGTH = 300


class Adjudication(BaseModel):
    """What the model may return: a sentence, and a confidence it may lower."""

    reason_text: str
    confidence: float | None = None


PROMPT = """\
Two facts were extracted from different documents and compared. The comparison
has already been decided by deterministic rules. Your only job is to explain
that decision in one clear sentence a reader can check against the evidence.

You must NOT change the verdict. You must NOT introduce any fact, figure, date
or entity that does not appear below. Write about these two facts only.

VERDICT (already decided, do not change): {verdict}
REASON CODE (why the rules decided that): {reason_code}
CONTEXT FIELDS THAT DIFFER: {differing}

FACT A
  subject:   {subject_a}
  attribute: {attribute_a}
  value:     {value_a}
  period:    {period_a}
  scope:     {scope_a}
  basis:     {basis_a}
  vintage:   {vintage_a}
  source:    {source_a}, page {page_a}
  evidence:  "{evidence_a}"

FACT B
  subject:   {subject_b}
  attribute: {attribute_b}
  value:     {value_b}
  period:    {period_b}
  scope:     {scope_b}
  basis:     {basis_b}
  vintage:   {vintage_b}
  source:    {source_b}, page {page_b}
  evidence:  "{evidence_b}"

Return a JSON object with:
- "reason_text": one sentence, at most {max_length} characters, explaining the
  verdict in plain language. Name the specific difference where there is one.
- "confidence": optionally a number from 0 to 1. Provide it only to LOWER the
  confidence below {confidence:.2f} when the case is genuinely marginal - for
  example when the two facts may not really be measuring the same thing, or
  the evidence is too thin to support the comparison. Omit it otherwise. It
  will be ignored if higher than {confidence:.2f}.
"""


def _describe(value: str | None) -> str:
    return value if value else "not stated"


def build_prompt(verdict: Verdict) -> str:
    first, second = verdict.fact_a, verdict.fact_b
    signature_a, signature_b = first.signature, second.signature

    def context(signature, fact, attribute_name):
        if signature is not None:
            return _describe(getattr(signature, attribute_name))
        return _describe(getattr(fact.context, attribute_name))

    def period(signature, fact):
        if signature is not None and signature.period.raw:
            return signature.period.raw
        return _describe(fact.context.period)

    return PROMPT.format(
        verdict=verdict.verdict,
        reason_code=verdict.reason_code,
        differing=", ".join(verdict.differing_fields) or "none",
        max_length=MAX_REASON_LENGTH,
        confidence=verdict.confidence,
        subject_a=first.subject,
        attribute_a=first.attribute,
        value_a=f"{first.value_raw} {first.unit or ''}".strip(),
        period_a=period(signature_a, first),
        scope_a=context(signature_a, first, "scope"),
        basis_a=context(signature_a, first, "basis"),
        vintage_a=context(signature_a, first, "vintage"),
        source_a=first.source_doc,
        page_a=first.page,
        evidence_a=first.evidence_span,
        subject_b=second.subject,
        attribute_b=second.attribute,
        value_b=f"{second.value_raw} {second.unit or ''}".strip(),
        period_b=period(signature_b, second),
        scope_b=context(signature_b, second, "scope"),
        basis_b=context(signature_b, second, "basis"),
        vintage_b=context(signature_b, second, "vintage"),
        source_b=second.source_doc,
        page_b=second.page,
        evidence_b=second.evidence_span,
    )


def parse_adjudication(text: str) -> dict:
    """Read the model's reply, tolerating fences and surrounding prose."""
    if not text or not text.strip():
        raise FactJSONError("empty adjudication response")

    from backend.pipeline.extraction import (
        extract_json_array,
        extract_json_object,
        strip_code_fences,
    )

    candidate = strip_code_fences(text)
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        # An object is what was asked for; an array is a common slip.
        block = extract_json_object(candidate) or extract_json_array(candidate)
        if block is None:
            raise FactJSONError("no JSON in adjudication response") from None
        try:
            payload = json.loads(block)
        except json.JSONDecodeError as exc:
            raise FactJSONError(f"malformed adjudication JSON: {exc}") from exc

    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    if not isinstance(payload, dict):
        raise FactJSONError("adjudication was not an object")

    return payload


def apply_adjudication(verdict: Verdict, payload: dict) -> bool:
    """Take what the model may give, and refuse what it may not.

    Returns whether anything was accepted. The verdict itself is never taken
    from the payload; a reply that argues for a different one is dropped.
    """
    proposed_verdict = payload.get("verdict")
    if proposed_verdict and str(proposed_verdict).strip() != verdict.verdict:
        # The model tried to overturn a decided verdict. Keep the rules' answer.
        return False

    reason = payload.get("reason_text")
    accepted = False

    if isinstance(reason, str) and reason.strip():
        cleaned = " ".join(reason.split())[:MAX_REASON_LENGTH]
        verdict.reason_text = cleaned
        accepted = True

    proposed_confidence = payload.get("confidence")
    if proposed_confidence is not None:
        try:
            value = float(proposed_confidence)
        except (TypeError, ValueError):
            value = None
        if value is not None:
            # Only ever downwards, and never outside [0, 1].
            verdict.confidence = min(verdict.confidence, max(0.0, min(1.0, value)))
            accepted = True

    verdict.adjudicated = accepted
    return accepted


def adjudicate(verdict: Verdict, client=None, *, model: str | None = None, sleep=time.sleep) -> bool:
    """Ask the model to explain one verdict. Failure leaves the rules' text."""
    from backend.pipeline.extraction import generate_text

    try:
        raw = generate_text(
            client,
            build_prompt(verdict),
            model=model or config.GEMINI_EXTRACTION_MODEL,
            response_schema=Adjudication,
            sleep=sleep,
        )
        payload = parse_adjudication(raw)
    except (FactJSONError, json.JSONDecodeError, IndexError):
        return False
    except Exception:
        # A model failure must never cost the verdict; the rules already have it.
        return False

    return apply_adjudication(verdict, payload)


def adjudicate_all(
    verdicts: list[Verdict], client=None, *, model: str | None = None, sleep=time.sleep
) -> int:
    """Explain each verdict, returning how many the model actually improved."""
    if client is None:
        if not config.has_gemini_key():
            # No key: every verdict keeps the explanation the rules wrote.
            return 0
        from backend.pipeline.extraction import get_client

        client = get_client()

    return sum(
        1
        for verdict in verdicts
        if adjudicate(verdict, client, model=model, sleep=sleep)
    )
