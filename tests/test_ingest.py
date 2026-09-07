"""End-to-end ingestion against the sample PDF.

Every page number asserted here is one grounding worked out by searching the
document. Nothing in the scripted replies tells the pipeline where a quote
lives, so these tests fail if grounding ever starts guessing.
"""

import json

import pytest

from backend import store
from backend.pipeline.ingest import ingest_pdf, sha256_of
from tests.conftest import ScriptedClient, quote_on, scripted_replies


def test_ingest_grounds_every_quote_to_its_own_page(db, fixture_path, parsed_doc):
    replies, expected = scripted_replies(parsed_doc)
    client = ScriptedClient(replies)

    report = ingest_pdf(fixture_path, "sample", db, client=client)

    assert report.proposed == len(expected)
    assert report.grounded == len(expected)
    assert report.dropped_ungrounded == 0

    located = {fact.evidence_span: fact.page for fact in report.facts}
    for quote, page_number in expected:
        assert located[quote] == page_number


def test_stored_facts_carry_their_evidence(db, fixture_path, parsed_doc):
    replies, _ = scripted_replies(parsed_doc)

    ingest_pdf(fixture_path, "sample", db, client=ScriptedClient(replies))

    facts = store.list_facts(db, "sample")
    assert facts
    for fact in facts:
        assert fact.is_grounded
        assert fact.page >= 1
        assert fact.rects, "a grounded fact must know where on the page it sits"
        assert fact.grounding_method in {"search_for", "normalized", "fuzzy"}
        assert fact.source_doc == fixture_path.stem
        assert fact.evidence_span.strip()


def test_value_num_is_left_for_normalization(db, fixture_path, parsed_doc):
    """Phase 2 stores what the document said; it does not convert anything."""
    replies, _ = scripted_replies(parsed_doc)

    ingest_pdf(fixture_path, "sample", db, client=ScriptedClient(replies))

    assert all(fact.value_num is None for fact in store.list_facts(db, "sample"))


def test_an_invented_quote_is_rejected_before_grounding(db, fixture_path, parsed_doc):
    """A quote the page does not contain is refused at the verbatim gate.

    The prompt demands a character-for-character span, so a fabricated one is
    discarded before it can even be looked for in the document.
    """
    invented = json.dumps(
        [
            {
                "subject": "Sample Entity",
                "attribute": "measure",
                "value_raw": "1",
                "evidence_span": "Grimsby quantum ferret amortisation schedule",
                "confidence": 0.99,
            }
        ]
    )
    client = ScriptedClient([invented] + ["[]"] * parsed_doc.page_count)

    report = ingest_pdf(fixture_path, "sample", db, client=client)

    assert report.unverbatim == 1
    assert report.proposed == 0
    assert report.grounded == 0
    assert store.count_facts(db, "sample") == 0


def test_a_quote_that_cannot_be_placed_is_still_dropped(db, fixture_path, parsed_doc):
    """Grounding remains the second line of defence, below the verbatim gate."""
    from backend.pipeline.ingest import build_fact

    fact = build_fact(
        {
            "subject": "Sample Entity",
            "attribute": "measure",
            "value_raw": "1",
            "evidence_span": "Grimsby quantum ferret amortisation schedule",
            "confidence": 0.99,
        },
        collection_id="sample",
        doc=parsed_doc,
        doc_id="d_test",
        page_index=0,
    )

    assert fact is None


def test_reingesting_the_same_document_is_skipped(db, fixture_path, parsed_doc):
    replies, expected = scripted_replies(parsed_doc)
    ingest_pdf(fixture_path, "sample", db, client=ScriptedClient(replies))
    before = store.count_facts(db, "sample")

    second = ingest_pdf(fixture_path, "sample", db, client=ScriptedClient([]))

    assert second.skipped
    assert store.count_facts(db, "sample") == before


def test_forcing_a_reingest_updates_rather_than_duplicates(db, fixture_path, parsed_doc):
    replies, _ = scripted_replies(parsed_doc)
    first = ingest_pdf(fixture_path, "sample", db, client=ScriptedClient(replies))
    before = store.count_facts(db, "sample")

    replies_again, _ = scripted_replies(parsed_doc)
    second = ingest_pdf(
        fixture_path,
        "sample",
        db,
        client=ScriptedClient(replies_again),
        skip_if_present=False,
    )

    assert not second.skipped
    assert store.count_facts(db, "sample") == before
    assert {f.fact_id for f in first.facts} == {f.fact_id for f in second.facts}


def test_identical_facts_within_a_document_collapse(db, fixture_path, parsed_doc):
    """The same fact proposed twice on a page is one fact, not two."""
    quote = quote_on(parsed_doc.page(0))
    entry = {
        "subject": "Sample Entity",
        "attribute": "measure",
        "value_raw": "1",
        "evidence_span": quote,
        "confidence": 0.9,
    }
    client = ScriptedClient([json.dumps([entry, dict(entry)])])

    report = ingest_pdf(fixture_path, "sample", db, client=client, max_pages=1)

    assert report.proposed == 2
    assert report.duplicates == 1
    assert report.grounded == 1


def test_a_paraphrased_quote_is_rejected(db, fixture_path, parsed_doc, monkeypatch):
    """Words lifted from the page but re-spaced are not an exact span."""
    monkeypatch.setattr("backend.config.REQUIRE_EXACT_SPANS", True)
    page = parsed_doc.page(0)
    reworded = " ".join(quote_on(page, 10, 14).split())  # collapsed whitespace
    payload = json.dumps(
        [
            {
                "subject": "Sample Entity",
                "attribute": "measure",
                "value_raw": "1",
                "evidence_span": reworded,
                "confidence": 0.9,
            }
        ]
    )
    client = ScriptedClient([payload] + ["[]"] * parsed_doc.page_count)

    report = ingest_pdf(fixture_path, "sample", db, client=client, max_pages=1)

    if reworded != quote_on(page, 10, 14):
        assert report.unverbatim == 1
        assert report.grounded == 0


def test_a_page_that_fails_does_not_abandon_the_document(db, fixture_path, parsed_doc):
    """One unparseable page must not cost the facts on every other page."""
    good = scripted_replies(parsed_doc)[0][1]
    client = ScriptedClient(["garbage", "still garbage", good])

    report = ingest_pdf(fixture_path, "sample", db, client=client, max_pages=2)

    assert report.failed_pages
    assert report.grounded > 0


def test_max_pages_limits_the_work(db, fixture_path, parsed_doc):
    replies, _ = scripted_replies(parsed_doc)
    client = ScriptedClient(replies)

    report = ingest_pdf(fixture_path, "sample", db, client=client, max_pages=1)

    assert report.pages_processed == 1
    assert client.calls == 1


def test_document_and_collection_rows_are_recorded(db, fixture_path, parsed_doc):
    replies, _ = scripted_replies(parsed_doc)

    report = ingest_pdf(fixture_path, "sample", db, client=ScriptedClient(replies))

    document = db.execute(
        "SELECT * FROM documents WHERE doc_id = ?", (report.doc_id,)
    ).fetchone()
    assert document["sha256"] == sha256_of(fixture_path)
    assert document["page_count"] == parsed_doc.page_count
    assert document["collection_id"] == "sample"

    collection = db.execute("SELECT * FROM collections").fetchone()
    assert collection["collection_id"] == "sample"


def test_collections_are_isolated(db, fixture_path, parsed_doc):
    replies_a, _ = scripted_replies(parsed_doc)
    replies_b, _ = scripted_replies(parsed_doc)

    ingest_pdf(fixture_path, "one", db, client=ScriptedClient(replies_a))
    ingest_pdf(fixture_path, "two", db, client=ScriptedClient(replies_b))

    assert store.count_facts(db, "one") > 0
    assert store.count_facts(db, "one") == store.count_facts(db, "two")
    assert {f.fact_id for f in store.list_facts(db, "one")}.isdisjoint(
        f.fact_id for f in store.list_facts(db, "two")
    )


def test_ingest_without_a_key_fails_loudly(db, fixture_path, monkeypatch):
    """No key must be an explicit error, never a silent empty result."""
    monkeypatch.setattr("backend.config.GEMINI_API_KEY", "")

    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        ingest_pdf(fixture_path, "sample", db)


def test_only_the_requested_pages_are_read(db, fixture_path, parsed_doc):
    """A run can target where the facts are instead of paying for a document."""
    wanted = parsed_doc.page(1)  # page number 2
    reply = json.dumps(
        [
            {
                "subject": "Sample Entity",
                "attribute": "measure",
                "value_raw": "1",
                "evidence_span": quote_on(wanted, 10, 14),
                "confidence": 0.9,
            }
        ]
    )
    client = ScriptedClient([reply])

    report = ingest_pdf(fixture_path, "sample", db, client=client, pages={2})

    assert report.pages_processed == 1
    assert client.calls == 1, "only the requested page costs a call"
    assert {fact.page for fact in report.facts} == {2}


def test_requesting_pages_a_document_does_not_have_reads_nothing(db, fixture_path, parsed_doc):
    client = ScriptedClient([])

    report = ingest_pdf(fixture_path, "sample", db, client=client, pages={9999})

    assert report.pages_processed == 0
    assert client.calls == 0


def test_a_finished_document_survives_a_later_failure(db, fixture_path, parsed_doc):
    """Extraction is expensive; one document's work must not be rolled back."""
    replies, _ = scripted_replies(parsed_doc)
    ingest_pdf(fixture_path, "sample", db, client=ScriptedClient(replies))

    # A fresh connection sees only what was committed.
    from backend import store as store_module

    other = store_module.connect(db.execute("PRAGMA database_list").fetchone()[2])
    try:
        assert store_module.count_facts(other, "sample") > 0
    finally:
        other.close()
