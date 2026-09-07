# Fact Knowledge Layer

Ingests PDFs, extracts grounded facts, and detects when facts across documents
corroborate, contradict, or reconcile through context.

Every fact is tied back to the page and the exact span of text it came from, so
any claim the system makes can be checked against the source document.

## Status

Phase 0 — skeleton only. The service boots and answers `/health`; parsing,
extraction, normalization, matching and reconciliation land in later phases.

## Layout

```
backend/            FastAPI app and configuration
backend/pipeline/   Ingestion pipeline: parsing, grounding, extraction,
                    normalization, matching, reconciliation
data/               Local SQLite database and uploaded PDFs (git-ignored)
tests/              Unit tests
```

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

## Test

```bash
pytest
```
