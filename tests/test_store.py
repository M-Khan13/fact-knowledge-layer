"""Persistence tests.

The facts used here come out of a real ingestion of the sample PDF, so the
pages and evidence spans being written and read back are ones grounding
actually produced.
"""

from backend import store
from backend.pipeline.ingest import ingest_pdf
from tests.conftest import ScriptedClient, scripted_replies


def ingest(db, fixture_path, parsed_doc, collection="sample"):
    replies, _ = scripted_replies(parsed_doc)
    return ingest_pdf(fixture_path, collection, db, client=ScriptedClient(replies))


def test_facts_round_trip_unchanged(db, fixture_path, parsed_doc):
    report = ingest(db, fixture_path, parsed_doc)
    original = {fact.fact_id: fact for fact in report.facts}

    for stored in store.list_facts(db, "sample"):
        source = original[stored.fact_id]
        assert stored.subject == source.subject
        assert stored.attribute == source.attribute
        assert stored.value_raw == source.value_raw
        assert stored.evidence_span == source.evidence_span
        assert stored.page == source.page
        assert stored.rects == source.rects
        assert stored.confidence == source.confidence


def test_context_survives_the_round_trip(db, fixture_path, parsed_doc):
    report = ingest(db, fixture_path, parsed_doc)
    fact = report.facts[0]
    fact.context.period = "some period as written"
    fact.context.scope = "some scope as written"
    store.save_facts(db, [fact])

    reloaded = store.get_fact(db, fact.fact_id)

    assert reloaded.context.period == "some period as written"
    assert reloaded.context.scope == "some scope as written"
    assert reloaded.context.basis is None
    assert reloaded.context.vintage is None


def test_saving_the_same_fact_twice_updates_in_place(db, fixture_path, parsed_doc):
    report = ingest(db, fixture_path, parsed_doc)
    before = store.count_facts(db, "sample")

    fact = report.facts[0]
    fact.value_num = 42.0
    fact.unit = "some_unit"
    store.save_facts(db, [fact])

    assert store.count_facts(db, "sample") == before
    reloaded = store.get_fact(db, fact.fact_id)
    assert reloaded.value_num == 42.0
    assert reloaded.unit == "some_unit"


def test_filters_narrow_the_result(db, fixture_path, parsed_doc):
    ingest(db, fixture_path, parsed_doc)
    everything = store.list_facts(db, "sample")

    by_attribute = store.list_facts(db, "sample", attribute="measure_0")
    assert by_attribute
    assert len(by_attribute) < len(everything)
    assert all(f.attribute == "measure_0" for f in by_attribute)

    by_subject = store.list_facts(db, "sample", subject="sample entity")
    assert len(by_subject) == len(everything), "subject filter is case-insensitive"

    assert store.list_facts(db, "sample", attribute="no_such_attribute") == []


def test_period_filter_matches_stored_context(db, fixture_path, parsed_doc):
    report = ingest(db, fixture_path, parsed_doc)
    fact = report.facts[0]
    fact.context.period = "Some Period"
    store.save_facts(db, [fact])

    assert [f.fact_id for f in store.list_facts(db, "sample", period="some period")] == [
        fact.fact_id
    ]


def test_unknown_collection_is_empty_not_an_error(db):
    assert store.list_facts(db, "nothing-here") == []
    assert store.count_facts(db, "nothing-here") == 0
    assert store.get_fact(db, "f_missing") is None


def test_results_are_ordered_by_document_position(db, fixture_path, parsed_doc):
    ingest(db, fixture_path, parsed_doc)

    pages = [fact.page for fact in store.list_facts(db, "sample")]

    assert pages == sorted(pages)


def test_normalized_fields_round_trip(db, fixture_path, parsed_doc):
    """The context signature must survive a trip through SQLite intact."""
    from backend.pipeline.normalization import normalize_fact

    report = ingest(db, fixture_path, parsed_doc)
    fact = report.facts[0]
    fact.unit = "₹ Cr"
    fact.value_raw = "8,142"
    fact.context.period = "FY 2023-24"
    fact.context.scope = "Consolidated"
    fact.context.vintage = "Audited"
    normalize_fact(fact)
    store.save_facts(db, [fact])

    reloaded = store.get_fact(db, fact.fact_id)

    assert reloaded.value_num == fact.value_num
    assert reloaded.unit_canonical == "INR"
    assert reloaded.unit_raw == "₹ Cr"
    assert reloaded.unit_family == "currency"
    assert reloaded.normalized is True
    assert reloaded.signature.period.fiscal_year == 2024
    assert reloaded.signature.period.period_type == "FY"
    assert reloaded.signature.scope == "consolidated"
    assert reloaded.signature.vintage == "final"
    assert reloaded.signature.key() == fact.signature.key()


def test_a_database_from_an_earlier_version_is_upgraded(tmp_path):
    """Opening an older database adds the new columns instead of failing."""
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE collections (collection_id TEXT PRIMARY KEY, name TEXT, created_at TEXT);
        CREATE TABLE documents (doc_id TEXT PRIMARY KEY, collection_id TEXT, source_doc TEXT,
            filename TEXT, sha256 TEXT, page_count INTEGER, created_at TEXT);
        CREATE TABLE facts (
            fact_id TEXT PRIMARY KEY, collection_id TEXT, doc_id TEXT, subject TEXT,
            subject_key TEXT, attribute TEXT, value_raw TEXT, value_num REAL, unit TEXT,
            context_period TEXT, context_scope TEXT, context_basis TEXT,
            context_vintage TEXT, source_doc TEXT, page INTEGER, page_label TEXT,
            evidence_span TEXT, rects TEXT, grounding_method TEXT, grounding_score REAL,
            confidence REAL, created_at TEXT);
        """
    )
    old.commit()
    old.close()

    conn = store.connect(path)
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(facts)")}
        assert {"unit_canonical", "sig_fiscal_year", "normalized"} <= columns
        assert store.list_facts(conn, "anything") == []
    finally:
        conn.close()
