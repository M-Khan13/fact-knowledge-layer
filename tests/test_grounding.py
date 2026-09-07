"""Parsing and grounding tests against the bundled sample PDF.

Every quote used here is read out of the fixture at test time rather than
written into the test. Nothing asserts a page number or a value that was typed
in by hand, so these tests check real round-trip behaviour instead of agreeing
with a guess.
"""

from pathlib import Path

import pytest

from backend.pipeline.grounding import (
    _try_normalized,
    ground,
    normalize_text,
    render_grounding,
    render_page,
)
from backend.pipeline.parsing import parse_pdf

FIXTURE = Path(__file__).parent / "fixtures" / "sample-report-excerpt.pdf"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture(scope="module")
def doc():
    with parse_pdf(FIXTURE) as parsed:
        yield parsed


def quote_from(page, start: int, length: int) -> str:
    """Build a verbatim quote from words actually on the page."""
    return " ".join(w.text for w in page.words[start : start + length])


def test_parses_pages_with_geometry(doc):
    assert doc.page_count > 0
    assert doc.source_doc == FIXTURE.stem

    for page in doc.pages:
        assert page.number == page.index + 1
        assert page.width > 0 and page.height > 0
        for word in page.words:
            assert word.x0 < word.x1 and word.y0 < word.y1
            assert 0 <= word.x0 and word.x1 <= page.width + 1


def test_finds_tables_somewhere_in_the_document(doc):
    """pdfplumber should recover structured rows from the fixture."""
    tables = [table for page in doc.pages for table in page.tables]

    assert tables, "expected at least one table in the sample document"
    assert any(rows >= 2 for rows, _cols in (t.shape for t in tables))


@pytest.mark.parametrize("page_index", [0, 1, 2])
def test_quote_grounds_back_to_its_own_page(doc, page_index):
    """A quote lifted from a page must be located on that same page."""
    page = doc.page(page_index)
    quote = quote_from(page, 5, 12)

    result = ground(quote, doc)

    assert result is not None
    assert result.page_index == page_index
    assert result.page_number == page_index + 1
    assert result.is_exact


def test_grounding_box_sits_inside_the_page(doc):
    page = doc.page(0)
    result = ground(quote_from(page, 5, 12), doc)

    x0, y0, x1, y1 = result.bbox
    assert x0 < x1 and y0 < y1
    assert x0 >= 0 and y0 >= 0
    assert x1 <= page.width and y1 <= page.height
    assert result.rects


def test_grounding_survives_whitespace_and_punctuation_drift(doc):
    """A model re-typing a quote changes spacing and quote characters."""
    page = doc.page(0)
    original = quote_from(page, 8, 14)
    drifted = "  ".join(original.replace("’", "'").split())

    result = ground(drifted, doc)

    assert result is not None
    assert result.page_index == 0


def test_multi_line_quote_highlights_each_line(doc):
    """A quote that wraps should yield one rect per line, not one huge box."""
    page = doc.page(0)
    long_quote = quote_from(page, 5, 45)

    result = ground(long_quote, doc)

    assert result is not None
    assert len(result.rects) > 1


def test_absent_quote_is_not_grounded(doc):
    """The system must refuse to place text that is not in the document."""
    assert ground("Grimsby quantum ferret amortisation schedule", doc) is None


def test_blank_quote_is_not_grounded(doc):
    assert ground("", doc) is None
    assert ground("   ", doc) is None


def test_render_page_returns_a_png(doc):
    png = render_page(doc, 0)

    assert png.startswith(PNG_MAGIC)


def test_render_highlight_changes_the_image(doc):
    """Highlighting must actually mark the page, not silently no-op."""
    result = ground(quote_from(doc.page(0), 5, 12), doc)

    plain = render_page(doc, 0)
    highlighted = render_grounding(doc, result)

    assert highlighted.startswith(PNG_MAGIC)
    assert highlighted != plain


def test_render_rejects_a_page_outside_the_document(doc):
    with pytest.raises(IndexError):
        render_page(doc, doc.page_count)


def test_lightly_paraphrased_quote_falls_back_to_fuzzy(doc):
    """A model that re-words one token still grounds, but is flagged inexact."""
    page = doc.page(0)
    original = quote_from(page, 5, 12)
    reworded = original.replace(original.split()[3], "notwithstanding", 1)

    result = ground(reworded, doc)

    assert result is not None
    assert result.method == "fuzzy"
    assert result.page_index == 0
    assert not result.is_exact
    assert result.score < 1.0


def test_fuzzy_floor_is_enforced(doc):
    """Raising the floor above the achievable score must reject the match."""
    page = doc.page(0)
    original = quote_from(page, 5, 12)
    reworded = original.replace(original.split()[3], "notwithstanding", 1)

    assert ground(reworded, doc, min_score=0.99) is None


def test_normalized_strategy_locates_a_quote_directly(doc):
    """search_for handles everything this fixture can throw at it, so the
    normalized tier is exercised directly rather than through a staged case.
    It earns its place on PDFs whose glyphs differ from a model's transcription.
    """
    page = doc.page(0)
    quote = quote_from(page, 5, 12)

    result = _try_normalized(page, quote)

    assert result is not None
    rects, matched_text, score = result
    assert score == 1.0
    assert rects
    assert normalize_text(quote) in normalize_text(matched_text)


def test_normalized_strategy_rejects_absent_text(doc):
    assert _try_normalized(doc.page(0), "quantum ferret amortisation") is None


def test_normalize_text_folds_the_noise():
    assert normalize_text("  Foo  Bar  ") == "foo bar"
    assert normalize_text("India\u2019s") == normalize_text("India's")
    assert normalize_text("2021\u201322") == normalize_text("2021-22")
    assert normalize_text("soft\u00adhyphen") == "softhyphen"
    assert normalize_text("non\u00a0breaking") == "non breaking"
    # Ligatures are why the normalized tier exists: PDFs carry them, models don't.
    assert normalize_text("\ufb01nancial") == "financial"
