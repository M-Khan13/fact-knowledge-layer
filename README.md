# Fact Knowledge Layer

Ingests PDFs, extracts grounded facts, and detects when facts across documents
corroborate, contradict, or reconcile through context.

Every fact is tied back to the page and the exact span of text it came from, so
any claim the system makes can be checked against the source document.

## Status

Phase 6 — the pipeline runs end to end behind a REST API: upload PDFs to a
collection and get back grounded, normalized facts and the relationships
between them, with the evidence for any fact served as an image of its page.
The eval harness lands next; the React UI is not built yet.

## Layout

```
backend/            FastAPI app and configuration
backend/pipeline/   Ingestion pipeline: parsing, grounding, extraction,
                    normalization, matching, reconciliation
backend/app.py      REST API and a minimal debug view
scripts/            Hand tools for inspecting the pipeline
data/               Local SQLite database and uploaded PDFs (git-ignored)
tests/              Unit tests, and the sample PDF they run against
```

## Grounding

A fact is only worth as much as the evidence behind it, so page numbers and
bounding boxes are never taken from a model. The extractor proposes a fact and
a verbatim quote; `ground()` then searches the parsed PDF for that quote and
reports where it physically sits. Three strategies run in order of how much
they can be trusted:

| Strategy | Catches |
|---|---|
| `search_for` | PyMuPDF's own search, including quotes that wrap across lines |
| `normalized` | quotes differing only in curly quotes, dashes, ligatures or spacing |
| `fuzzy` | light paraphrase or OCR drift, accepted only above a similarity floor |

A quote that matches nothing returns `None`. Refusing to place a quote is
always preferred to inventing a page for it.

Page numbers are physical positions in the file. A page label is reported only
when the PDF itself declares one — a number printed as ink on the page is not
read, because guessing it would fabricate provenance.

## Extraction

Each page is one request. The model is asked for a JSON array of facts and is
told to copy `value_raw` exactly as written and to quote `evidence_span`
verbatim from the page. It is explicitly not asked for a page number: that is
grounding's job.

A proposed fact becomes a stored fact only if its quote can be found in the
document. Grounding is attempted on the page the quote came from first, so a
sentence repeated across pages is not attributed to the wrong one. Anything
that cannot be placed is dropped and counted, never stored with a guessed page.

Responses are parsed defensively — code fences stripped, an array recovered
from surrounding prose, a single object or `{"facts": [...]}` wrapper accepted
— and a page whose JSON will not parse is retried once before being reported
as failed. Rate limits and transient server errors back off and retry. One bad
page never costs the rest of the document.

`value_num` and the canonical unit stay empty here; normalization fills them.

## Normalization

A fact is a value plus the conditions under which that value holds. Those
conditions — the **context signature** — are what comparison gates on, before
anything is decided about the numbers. Most false contradictions come from
comparing two figures that were never describing the same thing.

**Units.** Every value is stored in one base alongside the unit it was written
in, so `8,142 ₹ Cr` and `81,415.38 ₹ million` become the same number and agree
to within 0.006%. Unit-blind comparison is banned: a value whose unit cannot be
resolved gets no unit rather than a guessed one, has its confidence capped, and
can never be auto-contradicted. Units carry a *family*, and families never
cross-compare, so a percentage cannot contradict a headcount and rupees cannot
contradict dollars (no exchange rate is invented). A unit this code has never
seen becomes a canonical token of its own, so an unfamiliar document still
compares correctly with itself.

**Periods.** An Indian fiscal year runs April–March and is named for the year
it ends in. `FY2024/25`, `2024-25` and `FY25` therefore resolve to one token,
which is what lets different publishers line up at all. `year ended March 31,
2024` resolves to FY24 and `quarter ended December 31, 2023` to Q3 FY24.
Periods also nest: Q4 FY24 sits inside FY24, so a difference across that
boundary is a part against a whole, not a disagreement.

**Scope, basis, vintage.** Scope resolves to consolidated or standalone. Basis
stays free text, canonicalised but never constrained to a list. Vintage records
how settled a figure is, ordered `advance_estimate < provisional < revised <
final`, so a changed number across vintages is a revision rather than a
conflict. Projections sit outside that order.

**Entities.** Where a document states an official identifier — a DIN for a
person, a CIN for a company — that decides identity, since it survives spelling
differences a name does not. Both are matched by their published shape, not
against any list of known entities. Otherwise the normalized legal name is
used, with legal suffixes and honorifics dropped. A document's oblique
self-references ("the Company", "your Company") bind to whichever entity that
document actually names, worked out at runtime from its own facts.

**Agreement.** Two same-context values agree within the larger of ±0.5%
relative or one unit in the last reported decimal; percentage-style values use
a flat ±0.1 point band instead.

## Matching

Matching proposes candidate pairs; it does not judge them. The only question is
whether two facts are about the same subject and the same attribute — the
verdict comes later, from the context signature.

Pairs are always drawn **across different documents**. A document restating
itself is not cross-document agreement, and counting it would inflate
everything downstream.

Subjects are blocked on their resolved key. A stated DIN or CIN is
authoritative and must match exactly — two different DINs are two different
people however alike the names look. Only weaker name-based keys may merge on
similarity, which is where spelling variation actually occurs. Similarity is
scored per pair, so a block widened to catch a variant spelling does not
downgrade the pairs inside it that agree exactly.

Attributes are free text, so two documents rarely name a measure identically.
An exact match scores 1.0; otherwise similarity is measured by embedding. The
threshold is set low enough to admit pairs differing only in a qualifier —
"real GDP growth" against "nominal GDP growth" is one measure taken two ways,
and it is the `basis` field, not the threshold, that separates them afterwards.

Embeddings come from Gemini when a key is configured, and otherwise from a
deterministic local embedder using feature hashing, so matching still works and
stays reproducible offline. Vectors are cached by content in the database. The
local fallback is lexical rather than semantic — it relates "forex reserves" to
"foreign exchange reserves" but misses "CPI inflation" against "consumer price
inflation" — so **the thresholds are calibrated for it and should be re-tuned
against real embeddings**. Every threshold is a parameter for that reason.

Matching is incremental: passing the new document's fact ids restricts results
to pairs involving them, so adding a document never re-examines the pairs a
collection already had.

## Reconciliation

Every verdict is reached deterministically. A model never chooses one — it only
writes the sentence explaining a verdict already decided, and may lower the
confidence on a marginal case. The reason code proves the logic; the reason
text explains it.

The order of checks is the design:

1. **Units first.** Two values that cannot be compared are never contradicted,
   whatever the numbers say. A missing or unresolvable unit gives `no-verdict` /
   `unit_missing`; two units that both resolve but do not convert — a percentage
   against rupees, or rupees against dollars — give `unit_incomparable`. The two
   are kept apart because one is a gap in the source and the other a category
   error, which are different things to go and fix.
2. **Context next.** Facts holding under different conditions are
   `reconcilable`, however far apart their values are.
3. **Values last**, once the two facts are known to describe the same thing
   under the same conditions.

| Verdict | Reason code | When |
|---|---|---|
| `corroborate` | `same_value` | same context, values agree |
| `corroborate` | `unit_diff_resolved` | agree once both units resolve to one base |
| `contradict` | `value_conflict` | same context, values outside the band |
| `reconcilable` | `period_subset` | one period contains the other |
| `reconcilable` | `period_diff` | different, non-nested periods |
| `reconcilable` | `scope_diff` | consolidated against standalone |
| `reconcilable` | `basis_diff` | a different measure |
| `reconcilable` | `vintage_revision` | same period at a different stage of revision |
| `no-verdict` | `unit_missing` | one side states no unit that resolves |
| `no-verdict` | `unit_incomparable` | both units resolve but do not convert |

Where several context fields differ, period decides the reason — a figure for a
different span of time is a different figure whatever else also changed — and
every differing field is recorded alongside it.

**The adjudicator's limits are enforced in code, not asked for in a prompt.** A
reply proposing a different verdict is discarded; a confidence above the
deterministic one is clamped down. Every relationship carries an explanation
written by the rules before the model is ever called, so reconciliation works
with no API key at all — adjudication only improves the wording.

## Ingesting documents

```bash
python scripts/ingest.py --collection macro --input ~/path/to/pdfs
python scripts/ingest.py --collection macro --pdf one.pdf --pdf two.pdf --max-pages 5
```

Needs `GEMINI_API_KEY` in `.env`. Ingestion is per document and idempotent: a
document already in the collection is skipped, and re-ingesting updates rows
in place rather than duplicating them, so adding a document never rebuilds the
collection. `--max-pages` is worth using while experimenting, since a full
100-page report costs one model call per page.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # then fill in GEMINI_API_KEY
```

Secrets are read from `.env` via python-dotenv. `.env` is git-ignored and no
key is ever logged or returned by an endpoint.

## Run

```bash
uvicorn backend.app:app --reload
```

Interactive API docs are at `/docs`, and `/` is a bare debug view listing
collections, their relationships and links to the evidence images.

## API

| Endpoint | Purpose |
|---|---|
| `POST /collections` | Create a collection (or rename one) |
| `GET /collections` | List collections with document and fact counts |
| `GET /collections/{id}` | One collection, its documents and verdict counts |
| `POST /collections/{id}/documents` | Upload one or more PDFs and run the pipeline |
| `GET /collections/{id}/documents` | Documents in a collection |
| `GET /collections/{id}/facts` | Facts, filterable by `subject`, `attribute`, `period`, `doc_id` |
| `GET /collections/{id}/relationships` | Relationships, filterable by `verdict`, `reason_code`, `fact_id` |
| `POST /collections/{id}/reconcile` | Re-run reconciliation over a whole collection |
| `GET /facts/{id}` | One fact |
| `GET /facts/{id}/evidence` | The fact's page as a PNG, with its evidence boxed |
| `GET /health` | Liveness, and whether a key is configured |

Uploading runs the pipeline **for that document only**: it is parsed,
extracted, grounded and normalized, and then its new facts are matched against
the facts the collection already holds. Nothing is rebuilt, so a collection
stays cheap to add to. A document is identified by its content hash, so
re-uploading the same file — under any name — is recognised rather than
duplicated. Use `?max_pages=N` while experimenting.

```bash
curl -X POST localhost:8000/collections -H 'content-type: application/json' \
     -d '{"collection_id": "macro"}'

curl -X POST 'localhost:8000/collections/macro/documents?max_pages=5' \
     -F files=@report-one.pdf -F files=@report-two.pdf

curl 'localhost:8000/collections/macro/relationships?verdict=contradict'
curl localhost:8000/facts/f_abc123def456/evidence --output evidence.png
```

Processing is synchronous, and a full report is one model call per page — so a
100-page upload is a long request. `max_pages` keeps it manageable; a job queue
would be the next step if this needed to be interactive.

To locate a quote in any PDF and save the highlighted page:

```bash
python scripts/ground_quote.py --pdf path/to/report.pdf \
    --quote "some sentence from the document" --out hit.png
```

With no `--pdf` it reads the first PDF in `--input`, falling back to `INPUT_DIR`
from `.env`. No document path is baked into the code.

## Test

```bash
pytest
```
