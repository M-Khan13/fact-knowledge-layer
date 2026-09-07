"""PDF parsing: per-page text, per-word bounding boxes, and tables.

PyMuPDF supplies the text and word geometry that grounding searches against;
pdfplumber supplies table structure, which PyMuPDF does not model. Nothing here
interprets the content of a document - it only reports what is physically on
the page, so the same code works for any PDF.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber
import pymupdf

BBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class Word:
    """A single word and where it physically sits on the page."""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    block: int
    line: int
    index: int

    @property
    def bbox(self) -> BBox:
        return (self.x0, self.y0, self.x1, self.y1)

    @property
    def line_key(self) -> tuple[int, int]:
        """Identifies the text line this word belongs to, for grouping rects."""
        return (self.block, self.line)


@dataclass(frozen=True)
class Table:
    """A table as pdfplumber recovered it. Cells may be None where empty."""

    page_index: int
    rows: list[list[str | None]]
    bbox: BBox | None = None

    @property
    def shape(self) -> tuple[int, int]:
        return (len(self.rows), max((len(r) for r in self.rows), default=0))


@dataclass
class ParsedPage:
    """One page: its text, its words with geometry, and any tables on it."""

    index: int
    number: int
    label: str | None
    width: float
    height: float
    text: str
    words: list[Word] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)


@dataclass
class ParsedDoc:
    """A parsed PDF.

    Keeps the PyMuPDF handle open so grounding can call ``search_for`` and so
    pages can be rendered, rather than reopening the file for every lookup.
    Close it when done, or use it as a context manager.
    """

    source_doc: str
    path: Path
    pages: list[ParsedPage]
    handle: pymupdf.Document

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def page(self, index: int) -> ParsedPage:
        return self.pages[index]

    def close(self) -> None:
        if self.handle is not None and not self.handle.is_closed:
            self.handle.close()

    def __enter__(self) -> ParsedDoc:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def _page_labels(doc: pymupdf.Document) -> dict[int, str]:
    """Page labels the PDF itself declares, if any.

    Many PDFs declare none. In that case pages carry no label and grounding
    reports the physical page number instead. A printed number that only exists
    as ink on the page is not read here - guessing it would invent provenance.
    """
    try:
        labels = doc.get_page_labels()
    except Exception:
        return {}
    if not labels:
        return {}
    resolved: dict[int, str] = {}
    for index in range(doc.page_count):
        try:
            label = doc[index].get_label()
        except Exception:
            continue
        if label:
            resolved[index] = label
    return resolved


def _extract_words(page: pymupdf.Page) -> list[Word]:
    """Words with bounding boxes, in reading order as PyMuPDF reports them."""
    words: list[Word] = []
    for position, raw in enumerate(page.get_text("words")):
        x0, y0, x1, y1, text, block, line, _word_no = raw[:8]
        if not text.strip():
            continue
        words.append(
            Word(
                text=text,
                x0=float(x0),
                y0=float(y0),
                x1=float(x1),
                y1=float(y1),
                block=int(block),
                line=int(line),
                index=position,
            )
        )
    return words


def _extract_tables(path: Path, page_count: int) -> dict[int, list[Table]]:
    """Tables per page index, via pdfplumber.

    Table recovery is best-effort: a page that yields nothing simply has no
    tables recorded. A failure on one page must not abandon the whole document.
    """
    tables: dict[int, list[Table]] = {}
    try:
        with pdfplumber.open(path) as plumber_doc:
            for index, page in enumerate(plumber_doc.pages):
                if index >= page_count:
                    break
                try:
                    found = page.extract_tables()
                except Exception:
                    continue
                if not found:
                    continue
                tables[index] = [
                    Table(page_index=index, rows=[list(row) for row in rows])
                    for rows in found
                    if rows
                ]
    except Exception:
        return tables
    return tables


def parse_pdf(path: str | Path, *, with_tables: bool = True) -> ParsedDoc:
    """Parse a PDF into pages, words with geometry, and tables.

    ``source_doc`` is taken from the filename stem purely as a provenance label;
    no logic anywhere keys on it.
    """
    path = Path(path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"No PDF at {path}")

    handle = pymupdf.open(path)
    try:
        labels = _page_labels(handle)
        tables = _extract_tables(path, handle.page_count) if with_tables else {}

        pages: list[ParsedPage] = []
        for index in range(handle.page_count):
            page = handle[index]
            rect = page.rect
            pages.append(
                ParsedPage(
                    index=index,
                    number=index + 1,
                    label=labels.get(index),
                    width=float(rect.width),
                    height=float(rect.height),
                    text=page.get_text(),
                    words=_extract_words(page),
                    tables=tables.get(index, []),
                )
            )
    except Exception:
        handle.close()
        raise

    return ParsedDoc(source_doc=path.stem, path=path, pages=pages, handle=handle)
