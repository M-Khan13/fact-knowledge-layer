"""The fact object and its context signature.

This mirrors the fact schema exactly. `attribute` is deliberately free text
rather than an enum, so a document about something nobody anticipated simply
produces new attribute strings instead of failing to fit a fixed vocabulary.

Phase 2 fills the context fields with what the document actually says
("FY 2023-24", "Consolidated"). Normalization canonicalises them later; nothing
here interprets or converts a value.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field

from backend.pipeline.parsing import BBox
from backend.pipeline.temporal import Period


@dataclass(frozen=True)
class ContextSignature:
    """The normalized conditions under which a value holds.

    Comparison gates on this before any judgement about the numbers, which is
    what keeps a quarterly figure from contradicting an annual one.
    """

    period: Period = field(default_factory=Period)
    scope: str | None = None
    basis: str | None = None
    vintage: str | None = None

    def key(self) -> tuple:
        return (self.period.signature(), self.scope, self.basis, self.vintage)

    def differing_fields(self, other: ContextSignature) -> list[str]:
        """Which context fields disagree, in the order the rules consider them."""
        differences = []
        if self.period.signature() != other.period.signature():
            differences.append("period")
        if self.scope != other.scope:
            differences.append("scope")
        if self.basis != other.basis:
            differences.append("basis")
        if self.vintage != other.vintage:
            differences.append("vintage")
        return differences

    def matches(self, other: ContextSignature) -> bool:
        return not self.differing_fields(other)


@dataclass
class Context:
    """Under what conditions a value is true.

    Comparison gates on this signature, which is what stops "₹8,142 Cr" and
    "81,415 million", or FY and Q4 figures, from reading as contradictions.
    """

    period: str | None = None
    scope: str | None = None
    basis: str | None = None
    vintage: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)

    def is_empty(self) -> bool:
        return not any((self.period, self.scope, self.basis, self.vintage))


@dataclass
class Fact:
    """One extracted fact, tied to the place in the PDF it came from."""

    fact_id: str
    collection_id: str
    subject: str
    subject_key: str | None
    attribute: str
    value_raw: str
    value_num: float | None
    unit: str | None
    context: Context
    source_doc: str
    page: int | None
    evidence_span: str
    confidence: float

    # Provenance derived by code, never proposed by the model.
    doc_id: str | None = None
    page_label: str | None = None
    rects: list[BBox] = field(default_factory=list)
    grounding_method: str | None = None
    grounding_score: float | None = None

    # Filled by normalization. `unit` keeps the unit as the document wrote it;
    # `unit_canonical` is the base `value_num` is expressed in.
    unit_raw: str | None = None
    unit_canonical: str | None = None
    unit_family: str | None = None
    signature: ContextSignature | None = None
    normalized: bool = False

    @property
    def is_grounded(self) -> bool:
        return self.page is not None and self.grounding_method is not None

    @property
    def has_resolved_unit(self) -> bool:
        """Whether this fact may take part in a value comparison at all."""
        return self.unit_canonical is not None and self.value_num is not None

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["context"] = self.context.as_dict()
        return payload


def make_fact_id(
    collection_id: str,
    source_doc: str,
    attribute: str,
    value_raw: str,
    evidence_span: str,
) -> str:
    """A stable id derived from the fact's own content.

    Deriving it from content rather than a counter means re-ingesting the same
    document produces the same ids, so incremental ingestion can recognise a
    fact it already holds instead of duplicating it.
    """
    digest = hashlib.sha256(
        "\x1f".join(
            (collection_id, source_doc, attribute, value_raw, " ".join(evidence_span.split()))
        ).encode("utf-8")
    ).hexdigest()
    return f"f_{digest[:12]}"


def make_doc_id(collection_id: str, sha256: str) -> str:
    """Identifies a document by its bytes, so the same file re-uploads cleanly."""
    digest = hashlib.sha256(f"{collection_id}\x1f{sha256}".encode("utf-8")).hexdigest()
    return f"d_{digest[:12]}"
