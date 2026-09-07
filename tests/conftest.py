"""Shared test fixtures.

The pipeline needs a model to propose facts, so tests substitute a scripted
client for it. What that client returns is built from quotes read out of the
real sample PDF at test time - never a hand-written page number or a made-up
quote. Grounding still has to find each quote in the document on its own, so
the page numbers these tests assert on are derived, not asserted into being.
"""

import json
import re
from pathlib import Path

import pytest

from backend.pipeline.parsing import parse_pdf

FIXTURE = Path(__file__).parent / "fixtures" / "sample-report-excerpt.pdf"


class ScriptedClient:
    """Stands in for the Gemini client, returning queued replies in order."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []
        self.calls = 0
        self.models = self

    def generate_content(self, **kwargs):
        self.calls += 1
        self.prompts.append(kwargs.get("contents", ""))
        reply = self.replies.pop(0) if self.replies else "[]"
        if isinstance(reply, Exception):
            raise reply
        return type("Response", (), {"text": reply})()


@pytest.fixture(autouse=True)
def no_live_api_calls(monkeypatch):
    """Keep the suite hermetic whatever is in .env.

    With a real key configured the pipeline would reach for the live API -
    picking the Gemini embedder, or calling the adjudicator - which would make
    the tests slow, costly and non-deterministic. Every test that needs a model
    supplies a scripted one instead.
    """
    monkeypatch.setattr("backend.config.GEMINI_API_KEY", "", raising=False)


@pytest.fixture(scope="session")
def fixture_path() -> Path:
    return FIXTURE


@pytest.fixture(scope="session")
def parsed_doc():
    with parse_pdf(FIXTURE) as doc:
        yield doc


def quote_on(page, start: int = 10, length: int = 14) -> str:
    """A verbatim quote: a genuine substring of the page text.

    Sliced out of `page.text` rather than rebuilt by joining words, because
    extraction now discards any span that is not character-for-character
    present on the page — and a word-joined span is not, since the page text
    carries its own line breaks and spacing.
    """
    runs = list(re.finditer(r"\S+", page.text))
    if len(runs) <= start:
        start = 0
    chosen = runs[start : start + length]
    if not chosen:
        return ""
    return page.text[chosen[0].start() : chosen[-1].end()]


def scripted_replies(doc, per_page: int = 2):
    """One JSON reply per page, quoting text that really is on that page.

    The payloads carry no page numbers: grounding has to work out where each
    quote sits, which is the behaviour under test.
    """
    replies = []
    expected: list[tuple[str, int]] = []

    for page in doc.pages:
        facts = []
        for slot in range(per_page):
            quote = quote_on(page, start=10 + slot * 25, length=14)
            if not quote.strip():
                continue
            facts.append(
                {
                    "subject": "Sample Entity",
                    "attribute": f"measure_{slot}",
                    "value_raw": str(slot),
                    "context": {"period": None, "scope": None, "basis": None,
                                "vintage": None},
                    "evidence_span": quote,
                    "confidence": 0.9,
                }
            )
            expected.append((quote, page.number))
        replies.append(json.dumps(facts))

    return replies, expected


@pytest.fixture
def db(tmp_path):
    """A throwaway database for one test."""
    from backend import store

    conn = store.connect(tmp_path / "test.db")
    try:
        yield conn
    finally:
        conn.close()
