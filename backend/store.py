"""SQLite persistence for collections, documents and facts.

Plain sqlite3, no ORM - the shape is small and the queries are simple. Writes
are idempotent: re-ingesting a document or a fact updates the existing row
rather than creating a second copy, which is what lets ingestion be
incremental instead of a rebuild.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from backend import config
from backend.pipeline.schema import Context, ContextSignature, Fact
from backend.pipeline.temporal import Period

SCHEMA = """
CREATE TABLE IF NOT EXISTS collections (
    collection_id TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    doc_id        TEXT PRIMARY KEY,
    collection_id TEXT NOT NULL REFERENCES collections(collection_id),
    source_doc    TEXT NOT NULL,
    filename      TEXT NOT NULL,
    sha256        TEXT NOT NULL,
    page_count    INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    -- Where the file is, so evidence can be rendered from it later.
    path          TEXT,
    UNIQUE (collection_id, sha256)
);

CREATE TABLE IF NOT EXISTS facts (
    fact_id          TEXT PRIMARY KEY,
    collection_id    TEXT NOT NULL REFERENCES collections(collection_id),
    doc_id           TEXT REFERENCES documents(doc_id),
    subject          TEXT NOT NULL,
    subject_key      TEXT,
    attribute        TEXT NOT NULL,
    value_raw        TEXT NOT NULL,
    value_num        REAL,
    unit             TEXT,
    context_period   TEXT,
    context_scope    TEXT,
    context_basis    TEXT,
    context_vintage  TEXT,
    source_doc       TEXT NOT NULL,
    page             INTEGER,
    page_label       TEXT,
    evidence_span    TEXT NOT NULL,
    rects            TEXT,
    grounding_method TEXT,
    grounding_score  REAL,
    confidence       REAL NOT NULL,
    created_at       TEXT NOT NULL,
    -- Populated by normalization.
    unit_raw         TEXT,
    unit_canonical   TEXT,
    unit_family      TEXT,
    sig_period_type  TEXT,
    sig_fiscal_year  INTEGER,
    sig_sub_period   TEXT,
    sig_period_raw   TEXT,
    sig_scope        TEXT,
    sig_basis        TEXT,
    sig_vintage      TEXT,
    normalized       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_facts_collection ON facts(collection_id);
CREATE INDEX IF NOT EXISTS idx_facts_subject    ON facts(collection_id, subject_key);
CREATE INDEX IF NOT EXISTS idx_facts_attribute  ON facts(collection_id, attribute);
CREATE INDEX IF NOT EXISTS idx_facts_doc        ON facts(doc_id);
CREATE INDEX IF NOT EXISTS idx_facts_signature  ON facts(collection_id, subject_key, attribute);
"""

# Columns added after the first version of the table shipped. A database made
# by an earlier run is upgraded in place rather than being thrown away.
ADDED_COLUMNS: dict[str, str] = {
    "unit_raw": "TEXT",
    "unit_canonical": "TEXT",
    "unit_family": "TEXT",
    "sig_period_type": "TEXT",
    "sig_fiscal_year": "INTEGER",
    "sig_sub_period": "TEXT",
    "sig_period_raw": "TEXT",
    "sig_scope": "TEXT",
    "sig_basis": "TEXT",
    "sig_vintage": "TEXT",
    "normalized": "INTEGER NOT NULL DEFAULT 0",
}


ADDED_DOCUMENT_COLUMNS: dict[str, str] = {"path": "TEXT"}


def _migrate(conn: sqlite3.Connection) -> None:
    """Add any columns a database made by an older version is missing."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(facts)")}
    for column, declaration in ADDED_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE facts ADD COLUMN {column} {declaration}")

    existing = {row["name"] for row in conn.execute("PRAGMA table_info(documents)")}
    for column, declaration in ADDED_DOCUMENT_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE documents ADD COLUMN {column} {declaration}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open the database, creating its parent directory and schema if needed."""
    target = Path(path) if path is not None else config.DATABASE_PATH
    if str(target) != ":memory:":
        target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


@contextmanager
def session(path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_collection(conn: sqlite3.Connection, collection_id: str, name: str | None = None) -> str:
    conn.execute(
        """
        INSERT INTO collections (collection_id, name, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(collection_id) DO UPDATE SET name = excluded.name
        """,
        (collection_id, name or collection_id, _now()),
    )
    return collection_id


def upsert_document(
    conn: sqlite3.Connection,
    *,
    doc_id: str,
    collection_id: str,
    source_doc: str,
    filename: str,
    sha256: str,
    page_count: int,
    path: str | None = None,
) -> str:
    conn.execute(
        """
        INSERT INTO documents
            (doc_id, collection_id, source_doc, filename, sha256, page_count,
             created_at, path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(doc_id) DO UPDATE SET
            source_doc = excluded.source_doc,
            filename   = excluded.filename,
            page_count = excluded.page_count,
            path       = COALESCE(excluded.path, documents.path)
        """,
        (doc_id, collection_id, source_doc, filename, sha256, page_count, _now(), path),
    )
    return doc_id


def document_exists(conn: sqlite3.Connection, doc_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
    return row is not None


def save_facts(conn: sqlite3.Connection, facts: Iterable[Fact]) -> int:
    """Insert or refresh facts. Returns how many rows were written."""
    rows = [
        (
            fact.fact_id,
            fact.collection_id,
            fact.doc_id,
            fact.subject,
            fact.subject_key,
            fact.attribute,
            fact.value_raw,
            fact.value_num,
            fact.unit,
            fact.context.period,
            fact.context.scope,
            fact.context.basis,
            fact.context.vintage,
            fact.source_doc,
            fact.page,
            fact.page_label,
            fact.evidence_span,
            json.dumps(fact.rects),
            fact.grounding_method,
            fact.grounding_score,
            fact.confidence,
            _now(),
            fact.unit_raw,
            fact.unit_canonical,
            fact.unit_family,
            sig.period.period_type if sig else None,
            sig.period.fiscal_year if sig else None,
            sig.period.sub_period if sig else None,
            sig.period.raw if sig else None,
            sig.scope if sig else None,
            sig.basis if sig else None,
            sig.vintage if sig else None,
            1 if fact.normalized else 0,
        )
        for fact, sig in ((f, f.signature) for f in facts)
    ]
    if not rows:
        return 0

    conn.executemany(
        """
        INSERT INTO facts (
            fact_id, collection_id, doc_id, subject, subject_key, attribute,
            value_raw, value_num, unit, context_period, context_scope,
            context_basis, context_vintage, source_doc, page, page_label,
            evidence_span, rects, grounding_method, grounding_score,
            confidence, created_at, unit_raw, unit_canonical, unit_family,
            sig_period_type, sig_fiscal_year, sig_sub_period, sig_period_raw,
            sig_scope, sig_basis, sig_vintage, normalized
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(fact_id) DO UPDATE SET
            value_num       = excluded.value_num,
            unit            = excluded.unit,
            context_period  = excluded.context_period,
            context_scope   = excluded.context_scope,
            context_basis   = excluded.context_basis,
            context_vintage = excluded.context_vintage,
            page            = excluded.page,
            page_label      = excluded.page_label,
            rects           = excluded.rects,
            grounding_method= excluded.grounding_method,
            grounding_score = excluded.grounding_score,
            confidence      = excluded.confidence,
            unit_raw        = excluded.unit_raw,
            unit_canonical  = excluded.unit_canonical,
            unit_family     = excluded.unit_family,
            sig_period_type = excluded.sig_period_type,
            sig_fiscal_year = excluded.sig_fiscal_year,
            sig_sub_period  = excluded.sig_sub_period,
            sig_period_raw  = excluded.sig_period_raw,
            sig_scope       = excluded.sig_scope,
            sig_basis       = excluded.sig_basis,
            sig_vintage     = excluded.sig_vintage,
            normalized      = excluded.normalized
        """,
        rows,
    )
    return len(rows)


def _row_to_fact(row: sqlite3.Row) -> Fact:
    return Fact(
        fact_id=row["fact_id"],
        collection_id=row["collection_id"],
        subject=row["subject"],
        subject_key=row["subject_key"],
        attribute=row["attribute"],
        value_raw=row["value_raw"],
        value_num=row["value_num"],
        unit=row["unit"],
        context=Context(
            period=row["context_period"],
            scope=row["context_scope"],
            basis=row["context_basis"],
            vintage=row["context_vintage"],
        ),
        source_doc=row["source_doc"],
        page=row["page"],
        evidence_span=row["evidence_span"],
        confidence=row["confidence"],
        doc_id=row["doc_id"],
        page_label=row["page_label"],
        rects=[tuple(r) for r in json.loads(row["rects"] or "[]")],
        grounding_method=row["grounding_method"],
        grounding_score=row["grounding_score"],
        unit_raw=row["unit_raw"],
        unit_canonical=row["unit_canonical"],
        unit_family=row["unit_family"],
        normalized=bool(row["normalized"]),
        signature=ContextSignature(
            period=Period(
                period_type=row["sig_period_type"],
                fiscal_year=row["sig_fiscal_year"],
                sub_period=row["sig_sub_period"],
                raw=row["sig_period_raw"],
            ),
            scope=row["sig_scope"],
            basis=row["sig_basis"],
            vintage=row["sig_vintage"],
        )
        if row["normalized"]
        else None,
    )


def get_fact(conn: sqlite3.Connection, fact_id: str) -> Fact | None:
    row = conn.execute("SELECT * FROM facts WHERE fact_id = ?", (fact_id,)).fetchone()
    return _row_to_fact(row) if row else None


def list_facts(
    conn: sqlite3.Connection,
    collection_id: str,
    *,
    subject: str | None = None,
    attribute: str | None = None,
    period: str | None = None,
    doc_id: str | None = None,
    limit: int | None = None,
) -> list[Fact]:
    """Facts in a collection, optionally filtered. Filters are case-insensitive."""
    clauses = ["collection_id = ?"]
    params: list[object] = [collection_id]

    if subject:
        clauses.append("(LOWER(subject) LIKE ? OR LOWER(COALESCE(subject_key,'')) LIKE ?)")
        params += [f"%{subject.lower()}%", f"%{subject.lower()}%"]
    if attribute:
        clauses.append("LOWER(attribute) LIKE ?")
        params.append(f"%{attribute.lower()}%")
    if period:
        clauses.append("LOWER(COALESCE(context_period,'')) LIKE ?")
        params.append(f"%{period.lower()}%")
    if doc_id:
        clauses.append("doc_id = ?")
        params.append(doc_id)

    sql = f"SELECT * FROM facts WHERE {' AND '.join(clauses)} ORDER BY source_doc, page, fact_id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)

    return [_row_to_fact(row) for row in conn.execute(sql, params)]


def count_facts(conn: sqlite3.Connection, collection_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM facts WHERE collection_id = ?", (collection_id,)
    ).fetchone()
    return int(row["n"])


def list_collections(conn: sqlite3.Connection) -> list[dict]:
    """Every collection, with how much each one holds."""
    rows = conn.execute(
        """
        SELECT c.collection_id, c.name, c.created_at,
               (SELECT COUNT(*) FROM documents d WHERE d.collection_id = c.collection_id)
                   AS document_count,
               (SELECT COUNT(*) FROM facts f WHERE f.collection_id = c.collection_id)
                   AS fact_count
        FROM collections c
        ORDER BY c.created_at, c.collection_id
        """
    )
    return [dict(row) for row in rows]


def get_collection(conn: sqlite3.Connection, collection_id: str) -> dict | None:
    for row in list_collections(conn):
        if row["collection_id"] == collection_id:
            return row
    return None


def list_documents(conn: sqlite3.Connection, collection_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT d.*, (SELECT COUNT(*) FROM facts f WHERE f.doc_id = d.doc_id) AS fact_count
        FROM documents d WHERE d.collection_id = ?
        ORDER BY d.created_at, d.doc_id
        """,
        (collection_id,),
    )
    return [dict(row) for row in rows]


def get_document(conn: sqlite3.Connection, doc_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
    return dict(row) if row else None
