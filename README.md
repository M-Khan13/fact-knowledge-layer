# Fact Knowledge Layer

Ingests PDFs, extracts grounded facts, and detects when facts across documents
corroborate, contradict, or reconcile through context.

Every fact is tied back to the page and the exact span of text it came from, so
any claim the system makes can be checked against the source document.

## Status

Phase 1 — PDFs parse, and quotes ground back to the page and box they came
from. Extraction, normalization, matching and reconciliation land in later
phases.

## Layout

```
backend/            FastAPI app and configuration
backend/pipeline/   Ingestion pipeline: parsing, grounding, extraction,
                    normalization, matching, reconciliation
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

Then check the health probe:

```bash
curl http://127.0.0.1:8000/health
```

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
