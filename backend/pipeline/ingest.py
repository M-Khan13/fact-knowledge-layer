"""Ingest one PDF into a collection: parse, extract, ground, persist.

Order matters. The model proposes a fact and a quote; grounding then decides
where that quote physically sits. A fact whose quote cannot be found in the
document is dropped, because a fact nobody can check is worse than no fact.

Ingestion is per document and idempotent, so adding a document to a collection
never rebuilds the ones already in it.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from backend import store
from backend.pipeline.extraction import extract_page_facts
from backend.pipeline.grounding import DEFAULT_MIN_SCORE, ground
from backend.pipeline.normalization import normalize_facts
from backend.pipeline.parsing import ParsedDoc, parse_pdf
from backend.pipeline.schema import Context, Fact, make_doc_id, make_fact_id


@dataclass
class IngestReport:
    """What one document contributed, and what it cost to find out."""

    collection_id: str
    doc_id: str
    source_doc: str
    page_count: int
    pages_processed: int = 0
    proposed: int = 0
    grounded: int = 0
    dropped_ungrounded: int = 0
    duplicates: int = 0
    unresolved_units: int = 0
    unverbatim: int = 0
    failed_pages: list[int] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    skipped: bool = False

    @property
    def grounding_rate(self) -> float:
        return self.grounded / self.proposed if self.proposed else 0.0

    def summary(self) -> str:
        if self.skipped:
            return f"{self.source_doc}: already ingested, skipped"
        return (
            f"{self.source_doc}: {self.grounded} facts from {self.pages_processed} pages "
            f"({self.proposed} proposed, {self.dropped_ungrounded} dropped as ungrounded, "
            f"{self.duplicates} duplicate, {self.unverbatim} quotes not verbatim, "
            f"{len(self.failed_pages)} pages failed, "
            f"{self.unresolved_units} without a resolved unit)"
        )


def parse_page_ranges(spec: str | None) -> set[int] | None:
    """Read a page selection like "3-5,10,14" into the pages it names.

    Page numbers are 1-based, matching how a citation is written. None means
    every page.
    """
    if not spec or not spec.strip():
        return None

    pages: set[int] = set()
    for part in spec.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part.lstrip("-"):
            first, _, last = part.partition("-")
            try:
                start, end = int(first), int(last)
            except ValueError:
                raise ValueError(f"Not a page range: {part!r}") from None
            if start > end:
                start, end = end, start
            pages.update(range(start, end + 1))
        else:
            try:
                pages.add(int(part))
            except ValueError:
                raise ValueError(f"Not a page number: {part!r}") from None
    return pages or None


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def build_fact(
    raw: dict,
    *,
    collection_id: str,
    doc: ParsedDoc,
    doc_id: str,
    page_index: int,
    min_score: float = DEFAULT_MIN_SCORE,
) -> Fact | None:
    """Turn a proposed fact into a stored one, or drop it if it cannot be placed.

    Grounding is tried on the page the quote came from first. Only if that
    fails is the rest of the document searched, which keeps a sentence that
    repeats across pages from being attributed to the wrong one.
    """
    quote = raw["evidence_span"]

    location = ground(quote, doc, min_score=min_score, page_indices=[page_index])
    if location is None:
        location = ground(quote, doc, min_score=min_score)
    if location is None:
        return None

    context = Context(
        period=raw.get("period"),
        scope=raw.get("scope"),
        basis=raw.get("basis"),
        vintage=raw.get("vintage"),
    )

    confidence = raw["confidence"]
    if not location.is_exact:
        # The quote was only approximately found; say so in the confidence.
        confidence = min(confidence, location.score)

    return Fact(
        fact_id=make_fact_id(
            collection_id, doc.source_doc, raw["attribute"], raw["value_raw"], quote
        ),
        collection_id=collection_id,
        subject=raw["subject"],
        subject_key=raw.get("subject_key"),
        attribute=raw["attribute"],
        value_raw=raw["value_raw"],
        value_num=None,  # normalization populates this
        unit=raw.get("unit"),
        context=context,
        source_doc=doc.source_doc,
        page=location.page_number,
        evidence_span=quote,
        confidence=confidence,
        doc_id=doc_id,
        page_label=location.page_label,
        rects=location.rects,
        grounding_method=location.method,
        grounding_score=location.score,
    )


def ingest_pdf(
    path: str | Path,
    collection_id: str,
    conn: sqlite3.Connection,
    *,
    client=None,
    model: str | None = None,
    max_pages: int | None = None,
    pages: set[int] | None = None,
    min_score: float = DEFAULT_MIN_SCORE,
    skip_if_present: bool = True,
    on_page=None,
) -> IngestReport:
    """Parse, extract, ground and store the facts in one PDF."""
    path = Path(path).expanduser()
    checksum = sha256_of(path)
    doc_id = make_doc_id(collection_id, checksum)

    store.upsert_collection(conn, collection_id)

    if skip_if_present and store.document_exists(conn, doc_id):
        return IngestReport(
            collection_id=collection_id,
            doc_id=doc_id,
            source_doc=path.stem,
            page_count=0,
            skipped=True,
        )

    if client is None:
        from backend.pipeline.extraction import get_client

        client = get_client()

    with parse_pdf(path) as doc:
        store.upsert_document(
            conn,
            doc_id=doc_id,
            collection_id=collection_id,
            source_doc=doc.source_doc,
            filename=path.name,
            sha256=checksum,
            page_count=doc.page_count,
            path=str(path.resolve()),
        )

        report = IngestReport(
            collection_id=collection_id,
            doc_id=doc_id,
            source_doc=doc.source_doc,
            page_count=doc.page_count,
        )

        selected = doc.pages
        if pages is not None:
            # Only the pages asked for, so a run can target where facts live
            # instead of paying for a whole document.
            selected = [page for page in selected if page.number in pages]
        if max_pages is not None:
            selected = selected[:max_pages]
        seen: set[str] = set()

        for page in selected:
            result = extract_page_facts(
                client,
                page.text,
                page_index=page.index,
                doc_stem=doc.source_doc,
                page_number=page.number,
                model=model,
            )
            report.pages_processed += 1
            report.unverbatim += result.unverbatim
            if result.error:
                report.failed_pages.append(page.number)

            report.proposed += len(result.facts)

            for raw in result.facts:
                fact = build_fact(
                    raw,
                    collection_id=collection_id,
                    doc=doc,
                    doc_id=doc_id,
                    page_index=page.index,
                    min_score=min_score,
                )
                if fact is None:
                    report.dropped_ungrounded += 1
                    continue
                if fact.fact_id in seen:
                    report.duplicates += 1
                    continue
                seen.add(fact.fact_id)
                report.facts.append(fact)

            if on_page is not None:
                on_page(page, result, report)

        # Normalize as a batch: self-references resolve against the whole
        # document, which needs every fact from it in hand.
        normalize_facts(report.facts)

        report.grounded = len(report.facts)
        report.unresolved_units = sum(
            1 for fact in report.facts if not fact.has_resolved_unit
        )
        store.save_facts(conn, report.facts)

    return report
