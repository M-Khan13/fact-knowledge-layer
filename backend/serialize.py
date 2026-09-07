"""Turning stored objects into JSON the API can return.

Facts carry both what the document wrote and what normalization made of it.
Keeping the two side by side is deliberate: a reader can see the figure as
published and the value it was reduced to for comparison, which is what makes
a verdict checkable rather than merely stated.
"""

from __future__ import annotations

from backend.pipeline.schema import Fact


def period_to_dict(signature) -> dict | None:
    if signature is None:
        return None
    period = signature.period
    return {
        "period_type": period.period_type,
        "fiscal_year": period.fiscal_year,
        "sub_period": period.sub_period,
        "raw": period.raw,
    }


def signature_to_dict(signature) -> dict | None:
    """The normalized context a comparison actually gates on."""
    if signature is None:
        return None
    return {
        "period": period_to_dict(signature),
        "scope": signature.scope,
        "basis": signature.basis,
        "vintage": signature.vintage,
    }


def fact_to_dict(fact: Fact) -> dict:
    return {
        "fact_id": fact.fact_id,
        "collection_id": fact.collection_id,
        "doc_id": fact.doc_id,
        "subject": fact.subject,
        "subject_key": fact.subject_key,
        "attribute": fact.attribute,
        "value_raw": fact.value_raw,
        "value_num": fact.value_num,
        "unit": fact.unit,
        "unit_canonical": fact.unit_canonical,
        "unit_family": fact.unit_family,
        # As the document wrote it.
        "context": fact.context.as_dict(),
        # As normalization resolved it; this is what comparison uses.
        "signature": signature_to_dict(fact.signature),
        "source_doc": fact.source_doc,
        "page": fact.page,
        "page_label": fact.page_label,
        "evidence_span": fact.evidence_span,
        "confidence": fact.confidence,
        "grounding": {
            "method": fact.grounding_method,
            "score": fact.grounding_score,
            "rects": [list(rect) for rect in fact.rects],
        },
        "comparable": fact.has_resolved_unit,
        "evidence_url": f"/facts/{fact.fact_id}/evidence",
    }


def relationship_to_dict(row: dict, facts: dict[str, Fact] | None = None) -> dict:
    """A relationship, optionally with the two facts it is about inlined."""
    payload = {
        "relationship_id": row["relationship_id"],
        "collection_id": row["collection_id"],
        "verdict": row["verdict"],
        "reason_code": row["reason_code"],
        "reason_text": row["reason_text"],
        "confidence": row["confidence"],
        "match_score": row["match_score"],
        "differing_fields": row["differing_fields"],
        "adjudicated": bool(row["adjudicated"]),
        "fact_a": row["fact_a"],
        "fact_b": row["fact_b"],
    }

    if facts is not None:
        payload["facts"] = {
            side: fact_to_dict(facts[row[side]])
            for side in ("fact_a", "fact_b")
            if row[side] in facts
        }
    return payload


def ingest_report_to_dict(report) -> dict:
    return {
        "doc_id": report.doc_id,
        "source_doc": report.source_doc,
        "skipped": report.skipped,
        "page_count": report.page_count,
        "pages_processed": report.pages_processed,
        "facts_proposed": report.proposed,
        "facts_stored": report.grounded,
        "dropped_ungrounded": report.dropped_ungrounded,
        "duplicates": report.duplicates,
        "without_resolved_unit": report.unresolved_units,
        "failed_pages": report.failed_pages,
        "grounding_rate": round(report.grounding_rate, 4),
    }
