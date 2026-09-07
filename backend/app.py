"""FastAPI application entrypoint.

Phase 0 exposes only a health check. Ingestion, extraction and reconciliation
endpoints are added in later phases.
"""

from fastapi import FastAPI

from backend import config

app = FastAPI(
    title="Fact Knowledge Layer",
    description="Ingests PDFs, extracts grounded facts, and reconciles them across documents.",
    version="0.1.0",
)


@app.get("/health")
def health() -> dict:
    """Liveness probe plus a non-sensitive view of what is configured."""
    return {
        "status": "ok",
        "version": app.version,
        "gemini_key_configured": config.has_gemini_key(),
    }
