"""REST API over the fact knowledge layer.

Uploading a document runs the pipeline for that document only: it is parsed,
extracted, grounded and normalized, and then its new facts are matched against
the facts the collection already holds. Nothing is rebuilt, so a collection
grows a document at a time and stays cheap to add to.

Evidence is served as an image of the page with the supporting span boxed, so
any fact the API returns can be checked against the document it came from.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Iterator

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from backend import config, serialize, store
from backend.pipeline.grounding import render_page_from_file
from backend.pipeline.ingest import ingest_pdf
from backend.pipeline.reconciliation import (
    count_by_verdict,
    ensure_schema,
    list_relationships,
    reconcile,
)

PDF_MAGIC = b"%PDF"

app = FastAPI(
    title="Fact Knowledge Layer",
    description=(
        "Ingests PDFs, extracts grounded facts, and detects when facts across "
        "documents corroborate, contradict, or reconcile through context."
    ),
    version="0.6.0",
)


# The UI is served from its own dev-server port, so the browser treats it as a
# different origin and blocks the calls unless they are allowed here. Only the
# configured origins are permitted, never a wildcard.
if config.CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ORIGINS,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )


def get_db() -> Iterator[sqlite3.Connection]:
    """One connection per request; sqlite3 connections are not shared safely."""
    conn = store.connect()
    try:
        ensure_schema(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


class CollectionRequest(BaseModel):
    collection_id: str = Field(min_length=1, max_length=128)
    name: str | None = None


def _collection_or_404(conn: sqlite3.Connection, collection_id: str) -> dict:
    collection = store.get_collection(conn, collection_id)
    if collection is None:
        raise HTTPException(404, f"No collection '{collection_id}'")
    return collection


@app.get("/health")
def health() -> dict:
    """Liveness probe plus a non-sensitive view of what is configured."""
    return {
        "status": "ok",
        "version": app.version,
        "gemini_key_configured": config.has_gemini_key(),
    }


# --- Collections -----------------------------------------------------------


@app.post("/collections", status_code=201)
def create_collection(
    request: CollectionRequest, conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    """Create a collection, or rename one that already exists."""
    store.upsert_collection(conn, request.collection_id, request.name)
    return store.get_collection(conn, request.collection_id)


@app.get("/collections")
def get_collections(conn: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    return store.list_collections(conn)


@app.get("/collections/{collection_id}")
def get_collection(collection_id: str, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    collection = _collection_or_404(conn, collection_id)
    return {
        **collection,
        "documents": store.list_documents(conn, collection_id),
        "verdicts": count_by_verdict(conn, collection_id),
    }


# --- Documents -------------------------------------------------------------


def _save_upload(upload: UploadFile, destination: Path) -> Path:
    """Write an upload to disk, rejecting anything that is not a PDF."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        delete=False, dir=destination.parent, suffix=".part"
    ) as handle:
        temporary = Path(handle.name)
        shutil.copyfileobj(upload.file, handle)

    with open(temporary, "rb") as handle:
        if handle.read(len(PDF_MAGIC)) != PDF_MAGIC:
            temporary.unlink(missing_ok=True)
            raise HTTPException(400, f"'{upload.filename}' is not a PDF")

    temporary.replace(destination)
    return destination


@app.post("/collections/{collection_id}/documents", status_code=201)
def add_documents(
    collection_id: str,
    files: list[UploadFile] = File(..., description="One or more PDFs"),
    max_pages: int | None = Query(None, ge=1, description="Only read the first N pages"),
    force: bool = Query(False, description="Re-ingest a document already stored"),
    adjudicate: bool = Query(True, description="Let the model write reason texts"),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Add one or more PDFs and reconcile them against what is already here.

    Each document is processed on its own, and only its new facts are matched
    against the collection. Documents already present are skipped unless forced.
    """
    store.upsert_collection(conn, collection_id)

    reports: list[dict] = []
    new_fact_ids: set[str] = set()
    errors: list[dict] = []

    for upload in files:
        name = Path(upload.filename or "upload.pdf").name
        destination = config.UPLOAD_DIR / collection_id / name

        try:
            saved = _save_upload(upload, destination)
        except HTTPException as exc:
            errors.append({"filename": name, "error": exc.detail})
            continue
        finally:
            upload.file.close()

        try:
            report = ingest_pdf(
                saved,
                collection_id,
                conn,
                max_pages=max_pages,
                skip_if_present=not force,
            )
        except RuntimeError as exc:
            # No API key, most likely. Say so rather than failing opaquely.
            raise HTTPException(503, str(exc)) from exc

        reports.append(serialize.ingest_report_to_dict(report))
        new_fact_ids.update(fact.fact_id for fact in report.facts)

    # Match only the new facts against the collection; never rebuild.
    relationships = (
        reconcile(
            conn,
            collection_id,
            new_fact_ids=new_fact_ids,
            adjudicate_verdicts=adjudicate,
        )
        if new_fact_ids
        else []
    )

    return {
        "collection_id": collection_id,
        "documents": reports,
        "errors": errors,
        "new_facts": len(new_fact_ids),
        "new_relationships": len(relationships),
        "verdicts": count_by_verdict(conn, collection_id),
    }


@app.get("/collections/{collection_id}/documents")
def get_documents(
    collection_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> list[dict]:
    _collection_or_404(conn, collection_id)
    return store.list_documents(conn, collection_id)


# --- Facts -----------------------------------------------------------------


@app.get("/collections/{collection_id}/facts")
def get_facts(
    collection_id: str,
    subject: str | None = Query(None, description="Match subject or subject key"),
    attribute: str | None = None,
    period: str | None = Query(None, description="Match the period as written"),
    doc_id: str | None = None,
    limit: int | None = Query(None, ge=1, le=5000),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Facts in a collection. All filters are case-insensitive substrings."""
    _collection_or_404(conn, collection_id)

    facts = store.list_facts(
        conn,
        collection_id,
        subject=subject,
        attribute=attribute,
        period=period,
        doc_id=doc_id,
        limit=limit,
    )
    return {
        "collection_id": collection_id,
        "count": len(facts),
        "facts": [serialize.fact_to_dict(fact) for fact in facts],
    }


@app.get("/facts/{fact_id}")
def get_fact(fact_id: str, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    fact = store.get_fact(conn, fact_id)
    if fact is None:
        raise HTTPException(404, f"No fact '{fact_id}'")
    return serialize.fact_to_dict(fact)


@app.get(
    "/facts/{fact_id}/evidence",
    response_class=Response,
    responses={200: {"content": {"image/png": {}}}},
)
def get_fact_evidence(
    fact_id: str,
    zoom: float = Query(2.0, ge=0.5, le=6.0),
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    """The page this fact came from, as a PNG with its evidence boxed."""
    fact = store.get_fact(conn, fact_id)
    if fact is None:
        raise HTTPException(404, f"No fact '{fact_id}'")
    if fact.page is None:
        raise HTTPException(409, f"Fact '{fact_id}' is not grounded to a page")

    document = store.get_document(conn, fact.doc_id) if fact.doc_id else None
    if not document or not document.get("path"):
        raise HTTPException(410, "The source document is no longer on disk")

    path = Path(document["path"])
    if not path.is_file():
        raise HTTPException(410, f"Source document missing at {path}")

    try:
        png = render_page_from_file(path, fact.page - 1, fact.rects, zoom=zoom)
    except IndexError as exc:
        raise HTTPException(409, str(exc)) from exc

    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


# --- Relationships ---------------------------------------------------------


@app.get("/collections/{collection_id}/relationships")
def get_relationships(
    collection_id: str,
    verdict: str | None = Query(None, description="corroborate, contradict, ..."),
    reason_code: str | None = None,
    fact_id: str | None = None,
    include_facts: bool = Query(True, description="Inline the two facts"),
    limit: int | None = Query(None, ge=1, le=5000),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Relationships in a collection, most confident first."""
    _collection_or_404(conn, collection_id)

    rows = list_relationships(
        conn,
        collection_id,
        verdict=verdict,
        reason_code=reason_code,
        fact_id=fact_id,
        limit=limit,
    )

    facts: dict = {}
    if include_facts and rows:
        wanted = {row[side] for row in rows for side in ("fact_a", "fact_b")}
        facts = {
            fact.fact_id: fact
            for fact in store.list_facts(conn, collection_id)
            if fact.fact_id in wanted
        }

    return {
        "collection_id": collection_id,
        "count": len(rows),
        "verdicts": count_by_verdict(conn, collection_id),
        "relationships": [
            serialize.relationship_to_dict(row, facts if include_facts else None)
            for row in rows
        ],
    }


@app.post("/collections/{collection_id}/reconcile")
def rerun_reconciliation(
    collection_id: str,
    adjudicate: bool = Query(True),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Re-run matching and reconciliation over a whole collection.

    Adding documents already does this incrementally; this is for after a rule
    or threshold changes and the existing verdicts need revisiting.
    """
    _collection_or_404(conn, collection_id)
    verdicts = reconcile(conn, collection_id, adjudicate_verdicts=adjudicate)
    return {
        "collection_id": collection_id,
        "relationships": len(verdicts),
        "verdicts": count_by_verdict(conn, collection_id),
    }


# --- Minimal debug view ----------------------------------------------------


def _escape(value) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


@app.get("/", response_class=HTMLResponse)
def index(conn: sqlite3.Connection = Depends(get_db)) -> str:
    """A bare list of collections. The real interface is the REST API."""
    rows = "".join(
        f"<tr><td><a href='/debug/{_escape(c['collection_id'])}'>"
        f"{_escape(c['collection_id'])}</a></td>"
        f"<td>{c['document_count']}</td><td>{c['fact_count']}</td></tr>"
        for c in store.list_collections(conn)
    )
    return (
        "<title>Fact Knowledge Layer</title>"
        "<h1>Fact Knowledge Layer</h1>"
        "<p>Debug view. API docs at <a href='/docs'>/docs</a>.</p>"
        "<table border=1 cellpadding=6 cellspacing=0>"
        "<tr><th>Collection</th><th>Documents</th><th>Facts</th></tr>"
        f"{rows or '<tr><td colspan=3>No collections yet</td></tr>'}</table>"
    )


@app.get("/debug/{collection_id}", response_class=HTMLResponse)
def debug_collection(
    collection_id: str,
    limit: int = Query(100, ge=1, le=1000),
    conn: sqlite3.Connection = Depends(get_db),
) -> str:
    """Relationships and facts side by side, with links to the evidence images."""
    _collection_or_404(conn, collection_id)

    relationships = list_relationships(conn, collection_id, limit=limit)
    facts = store.list_facts(conn, collection_id, limit=limit)

    relationship_rows = "".join(
        f"<tr><td>{_escape(r['verdict'])}</td>"
        f"<td>{_escape(r['reason_code'])}</td>"
        f"<td>{r['confidence']:.2f}</td>"
        f"<td>{_escape(r['reason_text'])}</td>"
        f"<td><a href='/facts/{_escape(r['fact_a'])}/evidence'>A</a> "
        f"<a href='/facts/{_escape(r['fact_b'])}/evidence'>B</a></td></tr>"
        for r in relationships
    )

    fact_rows = "".join(
        f"<tr><td>{_escape(f.subject)}</td><td>{_escape(f.attribute)}</td>"
        f"<td>{_escape(f.value_raw)} {_escape(f.unit)}</td>"
        f"<td>{_escape(f.context.period)}</td>"
        f"<td>{_escape(f.source_doc)} p{f.page}</td>"
        f"<td><a href='/facts/{_escape(f.fact_id)}/evidence'>evidence</a></td></tr>"
        for f in facts
    )

    counts = count_by_verdict(conn, collection_id)
    return (
        f"<title>{_escape(collection_id)}</title>"
        f"<h1>{_escape(collection_id)}</h1>"
        f"<p><a href='/'>&larr; all collections</a> &middot; {_escape(counts)}</p>"
        "<h2>Relationships</h2>"
        "<table border=1 cellpadding=6 cellspacing=0>"
        "<tr><th>Verdict</th><th>Reason</th><th>Conf</th><th>Explanation</th>"
        "<th>Evidence</th></tr>"
        f"{relationship_rows or '<tr><td colspan=5>None yet</td></tr>'}</table>"
        "<h2>Facts</h2>"
        "<table border=1 cellpadding=6 cellspacing=0>"
        "<tr><th>Subject</th><th>Attribute</th><th>Value</th><th>Period</th>"
        "<th>Source</th><th></th></tr>"
        f"{fact_rows or '<tr><td colspan=6>None yet</td></tr>'}</table>"
    )
