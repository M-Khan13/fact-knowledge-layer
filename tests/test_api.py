"""API tests.

Uploads use the real sample PDF, and the scripted client quotes text read out
of it at test time, so every page number and evidence image the API returns was
worked out by grounding rather than written into the test.
"""

import json

import pytest
from fastapi.testclient import TestClient

from backend import store
from backend.app import app, get_db
from backend.pipeline import extraction
from tests.conftest import ScriptedClient, scripted_replies

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def client(tmp_path, monkeypatch, parsed_doc):
    """A client backed by a throwaway database and a scripted model."""
    database = tmp_path / "api.db"
    monkeypatch.setattr("backend.config.UPLOAD_DIR", tmp_path / "uploads")

    replies, _ = scripted_replies(parsed_doc)
    # Enough replies for several uploads in one test.
    monkeypatch.setattr(
        extraction, "get_client", lambda *a, **k: ScriptedClient(replies * 4)
    )

    def override():
        conn = store.connect(database)
        try:
            from backend.pipeline.reconciliation import ensure_schema

            ensure_schema(conn)
            yield conn
            conn.commit()
        finally:
            conn.close()

    app.dependency_overrides[get_db] = override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def pdf_bytes(fixture_path):
    return fixture_path.read_bytes()


@pytest.fixture
def distinct_pdfs(fixture_path):
    """Byte-distinct PDFs with identical text.

    Documents are identified by their content hash, so uploading the same
    bytes twice is correctly one document however it is named. Cross-document
    relationships therefore need genuinely different files.
    """
    import pymupdf

    def make(count: int) -> list[bytes]:
        variants = []
        for index in range(count):
            doc = pymupdf.open(fixture_path)
            doc.set_metadata({"title": f"variant-{index}"})
            variants.append(doc.tobytes())
            doc.close()
        return variants

    return make


def upload(client, collection, pdf_bytes, name="report.pdf", **params):
    return client.post(
        f"/collections/{collection}/documents",
        files={"files": (name, pdf_bytes, "application/pdf")},
        params=params,
    )


# --- Collections -----------------------------------------------------------


def test_health_still_answers(client):
    assert client.get("/health").json()["status"] == "ok"


def test_create_and_list_collections(client):
    created = client.post("/collections", json={"collection_id": "macro", "name": "Macro"})

    assert created.status_code == 201
    assert created.json()["collection_id"] == "macro"

    listed = client.get("/collections").json()
    assert [c["collection_id"] for c in listed] == ["macro"]
    assert listed[0]["fact_count"] == 0


def test_creating_the_same_collection_twice_renames_rather_than_duplicates(client):
    client.post("/collections", json={"collection_id": "macro", "name": "First"})
    client.post("/collections", json={"collection_id": "macro", "name": "Second"})

    listed = client.get("/collections").json()
    assert len(listed) == 1
    assert listed[0]["name"] == "Second"


def test_unknown_collection_is_a_404(client):
    assert client.get("/collections/nope").status_code == 404
    assert client.get("/collections/nope/facts").status_code == 404
    assert client.get("/collections/nope/relationships").status_code == 404


# --- Uploading -------------------------------------------------------------


def test_uploading_a_pdf_runs_the_pipeline(client, pdf_bytes):
    response = upload(client, "macro", pdf_bytes)

    assert response.status_code == 201
    body = response.json()
    assert body["documents"][0]["facts_stored"] > 0
    assert body["documents"][0]["dropped_ungrounded"] == 0
    assert body["new_facts"] == body["documents"][0]["facts_stored"]


def test_a_non_pdf_upload_is_rejected_without_killing_the_request(client, pdf_bytes):
    response = client.post(
        "/collections/macro/documents",
        files=[
            ("files", ("bad.pdf", b"not a pdf at all", "application/pdf")),
            ("files", ("good.pdf", pdf_bytes, "application/pdf")),
        ],
    )

    body = response.json()
    assert response.status_code == 201
    assert len(body["errors"]) == 1
    assert "not a PDF" in body["errors"][0]["error"]
    assert len(body["documents"]) == 1, "the valid file was still ingested"


def test_uploading_several_pdfs_at_once(client, pdf_bytes):
    response = client.post(
        "/collections/macro/documents",
        files=[
            ("files", ("one.pdf", pdf_bytes, "application/pdf")),
            ("files", ("two.pdf", pdf_bytes, "application/pdf")),
        ],
    )

    assert response.status_code == 201
    assert len(response.json()["documents"]) == 2


def test_the_same_document_twice_is_skipped(client, pdf_bytes):
    """Identity is the content hash, so renaming a file does not resubmit it."""
    first = upload(client, "macro", pdf_bytes, name="one.pdf")
    second = upload(client, "macro", pdf_bytes, name="renamed.pdf")

    assert first.json()["documents"][0]["skipped"] is False
    assert second.json()["documents"][0]["skipped"] is True
    assert second.json()["new_facts"] == 0


def test_max_pages_limits_the_work(client, pdf_bytes):
    response = upload(client, "macro", pdf_bytes, max_pages=1)

    assert response.json()["documents"][0]["pages_processed"] == 1


def test_documents_are_listed_after_upload(client, pdf_bytes):
    upload(client, "macro", pdf_bytes)

    documents = client.get("/collections/macro/documents").json()

    assert len(documents) == 1
    assert documents[0]["fact_count"] > 0
    assert documents[0]["path"], "the file location is recorded for evidence"


def test_uploading_without_a_key_reports_service_unavailable(
    client, pdf_bytes, monkeypatch
):
    """A missing key must be an explicit 503, not a silent empty result."""

    def refuse(*args, **kwargs):
        raise RuntimeError("No GEMINI_API_KEY configured.")

    monkeypatch.setattr(extraction, "get_client", refuse)

    response = upload(client, "macro", pdf_bytes)

    assert response.status_code == 503
    assert "GEMINI_API_KEY" in response.json()["detail"]


# --- Facts -----------------------------------------------------------------


def test_facts_are_returned_with_grounding_and_signature(client, pdf_bytes):
    upload(client, "macro", pdf_bytes)

    body = client.get("/collections/macro/facts").json()

    assert body["count"] > 0
    fact = body["facts"][0]
    assert fact["page"] >= 1
    assert fact["grounding"]["method"] in {"search_for", "normalized", "fuzzy"}
    assert fact["grounding"]["rects"]
    assert fact["evidence_span"]
    assert "signature" in fact and "context" in fact
    assert fact["evidence_url"] == f"/facts/{fact['fact_id']}/evidence"


def test_facts_can_be_filtered(client, pdf_bytes):
    upload(client, "macro", pdf_bytes)
    everything = client.get("/collections/macro/facts").json()["count"]

    by_attribute = client.get(
        "/collections/macro/facts", params={"attribute": "measure_0"}
    ).json()
    by_subject = client.get(
        "/collections/macro/facts", params={"subject": "sample entity"}
    ).json()
    nothing = client.get(
        "/collections/macro/facts", params={"attribute": "no_such_thing"}
    ).json()

    assert 0 < by_attribute["count"] < everything
    assert by_subject["count"] == everything
    assert nothing["count"] == 0


def test_limit_is_respected(client, pdf_bytes):
    upload(client, "macro", pdf_bytes)

    assert client.get("/collections/macro/facts", params={"limit": 2}).json()["count"] == 2


def test_a_single_fact_can_be_fetched(client, pdf_bytes):
    upload(client, "macro", pdf_bytes)
    fact_id = client.get("/collections/macro/facts").json()["facts"][0]["fact_id"]

    assert client.get(f"/facts/{fact_id}").json()["fact_id"] == fact_id
    assert client.get("/facts/f_missing").status_code == 404


# --- Evidence --------------------------------------------------------------


def test_evidence_returns_a_highlighted_png(client, pdf_bytes):
    upload(client, "macro", pdf_bytes)
    fact = client.get("/collections/macro/facts").json()["facts"][0]

    response = client.get(f"/facts/{fact['fact_id']}/evidence")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(PNG_MAGIC)


def test_evidence_differs_from_the_plain_page(client, pdf_bytes):
    """The highlight must actually be drawn, not silently skipped."""
    upload(client, "macro", pdf_bytes)
    facts = client.get("/collections/macro/facts").json()["facts"]
    on_page = [f for f in facts if f["page"] == facts[0]["page"]]

    first = client.get(f"/facts/{on_page[0]['fact_id']}/evidence").content
    second = client.get(f"/facts/{on_page[-1]['fact_id']}/evidence").content

    if on_page[0]["fact_id"] != on_page[-1]["fact_id"]:
        assert first != second, "different spans on one page must render differently"


def test_evidence_for_an_unknown_fact_is_a_404(client):
    assert client.get("/facts/f_nope/evidence").status_code == 404


def test_evidence_reports_a_missing_source_file(client, pdf_bytes, tmp_path):
    upload(client, "macro", pdf_bytes)
    fact = client.get("/collections/macro/facts").json()["facts"][0]

    for stored in (tmp_path / "uploads" / "macro").glob("*.pdf"):
        stored.unlink()

    assert client.get(f"/facts/{fact['fact_id']}/evidence").status_code == 410


# --- Relationships ---------------------------------------------------------


def test_relationships_appear_after_a_second_document(client, distinct_pdfs):
    one, two = distinct_pdfs(2)
    upload(client, "macro", one, name="one.pdf")
    upload(client, "macro", two, name="two.pdf")

    body = client.get("/collections/macro/relationships").json()

    assert body["count"] > 0
    relationship = body["relationships"][0]
    assert relationship["verdict"] in {
        "corroborate", "contradict", "reconcilable", "no-verdict"
    }
    assert relationship["reason_code"]
    assert relationship["reason_text"]
    assert relationship["facts"]["fact_a"]["source_doc"] != (
        relationship["facts"]["fact_b"]["source_doc"]
    )


def test_relationships_can_be_filtered_by_verdict(client, distinct_pdfs):
    one, two = distinct_pdfs(2)
    upload(client, "macro", one, name="one.pdf")
    upload(client, "macro", two, name="two.pdf")
    counts = client.get("/collections/macro/relationships").json()["verdicts"]

    for verdict, expected in counts.items():
        filtered = client.get(
            "/collections/macro/relationships", params={"verdict": verdict}
        ).json()
        assert filtered["count"] == expected


def test_relationships_can_be_returned_without_inlined_facts(client, distinct_pdfs):
    one, two = distinct_pdfs(2)
    upload(client, "macro", one, name="one.pdf")
    upload(client, "macro", two, name="two.pdf")

    body = client.get(
        "/collections/macro/relationships", params={"include_facts": False}
    ).json()

    assert body["count"] > 0
    assert "facts" not in body["relationships"][0]


def test_adding_a_document_matches_only_its_new_facts(client, distinct_pdfs):
    """The incremental promise: adding a document never rebuilds the rest."""
    one, two, three_bytes = distinct_pdfs(3)
    upload(client, "macro", one, name="one.pdf")
    upload(client, "macro", two, name="two.pdf")
    after_two = client.get("/collections/macro/relationships").json()["count"]

    third = upload(client, "macro", three_bytes, name="three.pdf")
    after_three = client.get("/collections/macro/relationships").json()["count"]

    assert third.json()["new_relationships"] > 0
    assert after_three > after_two
    # Every relationship the third upload created involves one of its facts.
    assert third.json()["new_relationships"] == after_three - after_two


def test_reconcile_can_be_rerun_over_a_whole_collection(client, distinct_pdfs):
    one, two = distinct_pdfs(2)
    upload(client, "macro", one, name="one.pdf")
    upload(client, "macro", two, name="two.pdf")
    before = client.get("/collections/macro/relationships").json()["count"]

    response = client.post("/collections/macro/reconcile")

    assert response.status_code == 200
    assert response.json()["relationships"] == before
    assert client.get("/collections/macro/relationships").json()["count"] == before


# --- Debug view ------------------------------------------------------------


def test_the_debug_view_renders(client, distinct_pdfs):
    one, two = distinct_pdfs(2)
    upload(client, "macro", one, name="one.pdf")
    upload(client, "macro", two, name="two.pdf")

    index = client.get("/")
    detail = client.get("/debug/macro")

    assert index.status_code == 200
    assert "macro" in index.text
    assert detail.status_code == 200
    assert "Relationships" in detail.text
    assert "/evidence" in detail.text


def test_the_debug_view_escapes_stored_text(client):
    """Text out of a document is data, not markup."""
    client.post("/collections", json={"collection_id": "<script>x</script>"})

    assert "<script>x</script>" not in client.get("/").text
    assert "&lt;script&gt;" in client.get("/").text


# --- Cross-origin access for the UI ----------------------------------------


def test_the_dev_ui_origin_is_allowed(client):
    """The React dev server runs on another port, so the browser needs this."""
    response = client.get("/health", headers={"Origin": "http://localhost:5173"})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_a_preflight_is_answered(client):
    response = client.options(
        "/collections/macro/facts",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_an_unlisted_origin_is_not_allowed(client):
    """Only the configured origins are let through, never anything that asks."""
    response = client.get("/health", headers={"Origin": "http://evil.example.com"})

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
