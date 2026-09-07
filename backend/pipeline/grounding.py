"""Evidence grounding: find where a quote physically sits in a parsed PDF.

The extractor proposes a fact and a verbatim quote; this module decides *where*
that quote lives. Page numbers and bounding boxes are therefore always derived
from the document, never taken from a model. If a quote cannot be located, the
answer is ``None`` - an ungrounded fact is better than an invented page.

Three strategies run in order of trustworthiness:

1. ``search_for`` - PyMuPDF's own text search, which handles line wrapping.
2. ``normalized``  - substring match over the page's word stream after unicode,
   punctuation and whitespace normalization. Catches quotes that differ only in
   curly quotes, dashes, ligatures or spacing.
3. ``fuzzy``       - best-matching window of words, accepted only above a
   similarity floor. Catches light paraphrase or OCR drift.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from io import BytesIO

import pymupdf
from PIL import Image, ImageDraw

from backend.pipeline.parsing import BBox, ParsedDoc, ParsedPage, Word

# Strategies ranked by how much we trust them; used to pick between candidates.
_METHOD_RANK = {"search_for": 3, "normalized": 2, "fuzzy": 1}

DEFAULT_MIN_SCORE = 0.85


def _build_char_map() -> dict[int, str]:
    """Fold the characters that should never decide whether a quote matches.

    Built from codepoints rather than literals so every entry is reviewable.
    """
    mapping: dict[int, str] = {}
    for cp in (0x2018, 0x2019, 0x201A, 0x201B, 0x2032):  # curly and prime quotes
        mapping[cp] = "'"
    for cp in (0x201C, 0x201D, 0x201E):  # curly double quotes
        mapping[cp] = '"'
    for cp in (0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212):  # dashes, minus
        mapping[cp] = "-"
    for cp in (0x00A0, 0x2005, 0x2006, 0x2007, 0x2009, 0x200A, 0x202F):  # exotic spaces
        mapping[cp] = " "
    for cp in (0x00AD, 0x200B, 0x200C, 0x200D, 0xFEFF):  # soft hyphen, zero-width
        mapping[cp] = ""
    return mapping


_CHAR_MAP = _build_char_map()


@dataclass(frozen=True)
class Grounding:
    """Where a quote was found, and how confident that location is."""

    page_index: int
    page_number: int
    page_label: str | None
    bbox: BBox
    rects: list[BBox]
    matched_text: str
    method: str
    score: float

    @property
    def is_exact(self) -> bool:
        return self.method != "fuzzy"


def normalize_text(text: str) -> str:
    """Fold away the differences that should never break a quote match."""
    folded = unicodedata.normalize("NFKC", text).translate(_CHAR_MAP)
    return " ".join(folded.casefold().split())


def _union(boxes: list[BBox]) -> BBox:
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _rects_from_words(words: list[Word]) -> list[BBox]:
    """One rect per text line, so a wrapped quote highlights as several bars."""
    lines: dict[tuple[int, int], list[BBox]] = {}
    order: list[tuple[int, int]] = []
    for word in words:
        key = word.line_key
        if key not in lines:
            lines[key] = []
            order.append(key)
        lines[key].append(word.bbox)
    return [_union(lines[key]) for key in order]


def _merge_search_rects(rects: list[pymupdf.Rect]) -> list[BBox]:
    """Collapse PyMuPDF's per-fragment hits into one rect per visual line."""
    lines: list[list[BBox]] = []
    for rect in sorted(rects, key=lambda r: (round(r.y0, 1), r.x0)):
        box = (float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1))
        centre = (box[1] + box[3]) / 2
        for line in lines:
            existing = _union(line)
            if existing[1] <= centre <= existing[3]:
                line.append(box)
                break
        else:
            lines.append([box])
    return [_union(line) for line in lines]


def _word_stream(words: list[Word]) -> tuple[str, list[str], list[Word], list[int]]:
    """Normalized page text plus the mapping back to the words that made it."""
    tokens: list[str] = []
    owners: list[Word] = []
    for word in words:
        token = normalize_text(word.text)
        if token:
            tokens.append(token)
            owners.append(word)

    starts: list[int] = []
    position = 0
    for token in tokens:
        starts.append(position)
        position += len(token) + 1

    return " ".join(tokens), tokens, owners, starts


def _words_in_span(
    tokens: list[str], owners: list[Word], starts: list[int], begin: int, end: int
) -> list[Word]:
    """Every word whose characters overlap the matched character span."""
    return [
        owner
        for i, owner in enumerate(owners)
        if starts[i] < end and starts[i] + len(tokens[i]) > begin
    ]


def _try_search_for(
    page: ParsedPage, handle: pymupdf.Document, quote: str
) -> tuple[list[BBox], str, float] | None:
    """PyMuPDF's own search, which already understands line wrapping."""
    try:
        hits = handle[page.index].search_for(quote, flags=pymupdf.TEXT_DEHYPHENATE)
    except Exception:
        return None
    if not hits:
        return None
    return _merge_search_rects(list(hits)), quote, 1.0


def _try_normalized(
    page: ParsedPage, quote: str
) -> tuple[list[BBox], str, float] | None:
    """Substring match once both sides are normalized the same way."""
    needle = normalize_text(quote)
    if not needle:
        return None
    haystack, tokens, owners, starts = _word_stream(page.words)
    found = haystack.find(needle)
    if found == -1:
        return None
    matched = _words_in_span(tokens, owners, starts, found, found + len(needle))
    if not matched:
        return None
    return _rects_from_words(matched), " ".join(w.text for w in matched), 1.0


def _try_fuzzy(
    page: ParsedPage, quote: str, min_score: float
) -> tuple[list[BBox], str, float] | None:
    """Best-scoring window of words, accepted only above the similarity floor."""
    needle = normalize_text(quote)
    if not needle:
        return None
    _haystack, tokens, owners, _starts = _word_stream(page.words)
    if not tokens:
        return None

    target = max(1, len(needle.split()))
    widths = {
        max(1, target + delta)
        for delta in (-2, -1, 0, 1, 2)
        if target + delta <= len(tokens)
    }
    if not widths:
        widths = {len(tokens)}

    matcher = SequenceMatcher(autojunk=False)
    matcher.set_seq2(needle)

    best: tuple[list[BBox], str, float] | None = None
    for width in sorted(widths):
        for start in range(0, len(tokens) - width + 1):
            window = " ".join(tokens[start : start + width])
            matcher.set_seq1(window)
            if matcher.real_quick_ratio() < min_score:
                continue
            if matcher.quick_ratio() < min_score:
                continue
            score = matcher.ratio()
            if score < min_score or (best is not None and score <= best[2]):
                continue
            matched = owners[start : start + width]
            best = (
                _rects_from_words(matched),
                " ".join(w.text for w in matched),
                score,
            )
    return best


def ground(
    quote: str, doc: ParsedDoc, *, min_score: float = DEFAULT_MIN_SCORE
) -> Grounding | None:
    """Locate ``quote`` in ``doc``, or return None if it is not really there.

    Returns the single best location across all pages: a more trustworthy
    strategy always beats a less trustworthy one, then a higher similarity
    score, then the earlier page.
    """
    if not quote or not quote.strip():
        return None

    candidates: list[Grounding] = []
    for page in doc.pages:
        # Evaluated lazily: the normalized pass only runs where search_for missed.
        for method, attempt in (
            ("search_for", lambda p=page: _try_search_for(p, doc.handle, quote)),
            ("normalized", lambda p=page: _try_normalized(p, quote)),
        ):
            result = attempt()
            if result is None:
                continue
            rects, matched_text, score = result
            candidates.append(
                Grounding(
                    page_index=page.index,
                    page_number=page.number,
                    page_label=page.label,
                    bbox=_union(rects),
                    rects=rects,
                    matched_text=matched_text,
                    method=method,
                    score=score,
                )
            )
            break

    # Fuzzy matching is expensive, so it only runs when nothing exact was found.
    if not candidates:
        for page in doc.pages:
            result = _try_fuzzy(page, quote, min_score)
            if result is None:
                continue
            rects, matched_text, score = result
            candidates.append(
                Grounding(
                    page_index=page.index,
                    page_number=page.number,
                    page_label=page.label,
                    bbox=_union(rects),
                    rects=rects,
                    matched_text=matched_text,
                    method="fuzzy",
                    score=score,
                )
            )

    if not candidates:
        return None

    return max(
        candidates,
        key=lambda g: (_METHOD_RANK[g.method], g.score, -g.page_index),
    )


def render_page(
    doc: ParsedDoc,
    page: int,
    bbox: BBox | list[BBox] | None = None,
    *,
    zoom: float = 2.0,
    colour: tuple[int, int, int] = (255, 202, 40),
    alpha: int = 90,
    padding: float = 1.5,
) -> bytes:
    """Render a page to PNG bytes, highlighting the given box or boxes."""
    if not 0 <= page < doc.page_count:
        raise IndexError(f"Page {page} out of range for {doc.source_doc}")

    pixmap = doc.handle[page].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
    image = Image.open(BytesIO(pixmap.tobytes("png"))).convert("RGBA")

    boxes: list[BBox] = []
    if bbox is not None:
        boxes = [bbox] if isinstance(bbox, tuple) else list(bbox)

    if boxes:
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        painter = ImageDraw.Draw(overlay)
        for box in boxes:
            x0, y0, x1, y1 = box
            scaled = (
                (x0 - padding) * zoom,
                (y0 - padding) * zoom,
                (x1 + padding) * zoom,
                (y1 + padding) * zoom,
            )
            painter.rectangle(scaled, fill=(*colour, alpha), outline=(*colour, 255), width=2)
        image = Image.alpha_composite(image, overlay)

    buffer = BytesIO()
    image.convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


def render_grounding(doc: ParsedDoc, grounding: Grounding, **kwargs) -> bytes:
    """Render the page a grounding points at, with its evidence highlighted."""
    return render_page(doc, grounding.page_index, grounding.rects, **kwargs)
