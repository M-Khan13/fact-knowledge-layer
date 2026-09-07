"""Deciding what a pair of facts means: agreement, conflict, or neither.

The verdict is reached deterministically, by comparing context signatures
before anything is decided about the numbers. A model never chooses the
verdict; it only writes the sentence that explains one, and may lower the
confidence where a case is genuinely marginal. That split is the point - the
reason code proves the logic, the reason text explains it.

The order of checks matters and is not arbitrary:

1. Units first. Two values that cannot be compared are never contradicted,
   whatever the numbers say.
2. Context next. Facts holding under different conditions are reconcilable,
   however far apart their values are.
3. Values last, and only once the two facts are known to describe the same
   thing under the same conditions.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field

from backend.pipeline.matching import CandidatePair, find_candidates
from backend.pipeline.normalization import (
    decimals_of,
    is_comparable,
    newer_vintage,
    values_agree,
)
from backend.pipeline.schema import Fact
from backend.pipeline.units import parse_unit

VERDICT_CORROBORATE = "corroborate"
VERDICT_CONTRADICT = "contradict"
VERDICT_RECONCILABLE = "reconcilable"
VERDICT_NO_VERDICT = "no-verdict"

REASON_SAME_VALUE = "same_value"
REASON_VALUE_CONFLICT = "value_conflict"
REASON_PERIOD_SUBSET = "period_subset"
REASON_PERIOD_DIFF = "period_diff"
REASON_SCOPE_DIFF = "scope_diff"
REASON_BASIS_DIFF = "basis_diff"
REASON_UNIT_DIFF_RESOLVED = "unit_diff_resolved"
REASON_VINTAGE_REVISION = "vintage_revision"

# Two distinct ways a comparison can be impossible, kept apart because they
# call for different fixes: one is a gap in the source, the other is a category
# error between two perfectly well-stated values.
REASON_UNIT_MISSING = "unit_missing"
REASON_UNIT_INCOMPARABLE = "unit_incomparable"

# Which context field decides the reason when more than one differs. Period is
# the most fundamental: a figure for a different span of time is a different
# figure whatever else also changed.
FIELD_PRECEDENCE = ("period", "scope", "basis", "vintage")

RELATIONSHIP_SCHEMA = """
CREATE TABLE IF NOT EXISTS relationships (
    relationship_id  TEXT PRIMARY KEY,
    collection_id    TEXT NOT NULL,
    fact_a           TEXT NOT NULL,
    fact_b           TEXT NOT NULL,
    verdict          TEXT NOT NULL,
    reason_code      TEXT NOT NULL,
    reason_text      TEXT,
    confidence       REAL NOT NULL,
    match_score      REAL,
    differing_fields TEXT,
    adjudicated      INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    UNIQUE (fact_a, fact_b)
);

CREATE INDEX IF NOT EXISTS idx_rel_collection ON relationships(collection_id);
CREATE INDEX IF NOT EXISTS idx_rel_verdict    ON relationships(collection_id, verdict);
CREATE INDEX IF NOT EXISTS idx_rel_fact_a     ON relationships(fact_a);
CREATE INDEX IF NOT EXISTS idx_rel_fact_b     ON relationships(fact_b);
"""


@dataclass
class Verdict:
    """What a pair of facts amounts to, and why."""

    fact_a: Fact
    fact_b: Fact
    verdict: str
    reason_code: str
    reason_text: str
    confidence: float
    differing_fields: list[str] = field(default_factory=list)
    match_score: float = 1.0
    adjudicated: bool = False

    @property
    def relationship_id(self) -> str:
        first, second = sorted((self.fact_a.fact_id, self.fact_b.fact_id))
        digest = hashlib.sha256(f"{first}\x1f{second}".encode("utf-8")).hexdigest()
        return f"r_{digest[:12]}"

    def as_dict(self) -> dict:
        return {
            "relationship_id": self.relationship_id,
            "fact_a": self.fact_a.fact_id,
            "fact_b": self.fact_b.fact_id,
            "verdict": self.verdict,
            "reason_code": self.reason_code,
            "reason_text": self.reason_text,
            "confidence": round(self.confidence, 4),
            "differing_fields": self.differing_fields,
            "match_score": round(self.match_score, 4),
            "adjudicated": self.adjudicated,
        }


def _format_value(fact: Fact) -> str:
    """The value as the document wrote it, with its unit."""
    written = fact.value_raw or ""
    unit = fact.unit or fact.unit_raw
    return f"{written} {unit}".strip() if unit else written


def _describe_period(fact: Fact) -> str:
    signature = fact.signature
    if signature is None or not signature.period.is_resolved:
        return (fact.context.period or "an unstated period").strip()
    period = signature.period
    if period.sub_period:
        return f"{period.sub_period} FY{period.fiscal_year}"
    if period.period_type == "CY":
        return f"CY{period.fiscal_year}"
    return f"FY{period.fiscal_year}"


def _units_written_differently(first: Fact, second: Fact) -> bool:
    """Whether the two facts wrote their unit differently but agree in base."""
    left = (first.unit or "").strip().lower()
    right = (second.unit or "").strip().lower()
    if not left or not right or left == right:
        return False
    return first.unit_canonical == second.unit_canonical


def _relative_gap(first: float, second: float) -> float:
    scale = max(abs(first), abs(second))
    return abs(first - second) / scale if scale else 0.0


def default_reason_text(verdict: Verdict) -> str:
    """A plain explanation built from the facts, with no model involved.

    This is what every relationship carries unless adjudication improves on it,
    so the system stays fully explainable with no API key at all.
    """
    first, second = verdict.fact_a, verdict.fact_b
    left, right = _format_value(first), _format_value(second)
    code = verdict.reason_code

    if code == REASON_UNIT_MISSING:
        unstated = first if not first.has_resolved_unit else second
        return (
            f"{left} and {right} cannot be compared: {unstated.source_doc} states "
            "no unit that resolves, so no verdict is possible."
        )

    if code == REASON_UNIT_INCOMPARABLE:
        return (
            f"{left} and {right} are measured in different units "
            f"({first.unit_canonical} against {second.unit_canonical}), which are "
            "not convertible here, so they cannot be compared."
        )

    if code == REASON_PERIOD_SUBSET:
        # The containing period is the outer one; the contained is the part.
        if first.signature.period.contains(second.signature.period):
            outer, inner = first, second
        else:
            outer, inner = second, first
        return (
            f"{_describe_period(outer)} covers {_describe_period(inner)}, so "
            f"{_format_value(inner)} is a part of {_format_value(outer)} "
            "rather than a disagreement with it."
        )

    if code == REASON_PERIOD_DIFF:
        return (
            f"These cover different periods, {_describe_period(first)} against "
            f"{_describe_period(second)}, so both values can hold."
        )

    if code == REASON_SCOPE_DIFF:
        return (
            f"One figure is {first.signature.scope or 'unscoped'} and the other "
            f"{second.signature.scope or 'unscoped'}, which is a different "
            "reporting boundary rather than a conflict."
        )

    if code == REASON_BASIS_DIFF:
        return (
            f"These use different measures, {first.signature.basis or 'unstated'} "
            f"against {second.signature.basis or 'unstated'}, so the values are "
            "not directly comparable."
        )

    if code == REASON_VINTAGE_REVISION:
        newer = newer_vintage(first.signature.vintage, second.signature.vintage)
        if newer:
            return (
                f"Same period reported at different stages; the {newer} figure "
                "supersedes the earlier one, so this is a revision."
            )
        return (
            f"Same period reported as {first.signature.vintage or 'unstated'} and "
            f"{second.signature.vintage or 'unstated'}, which is a difference in "
            "how settled the figure is."
        )

    if code == REASON_UNIT_DIFF_RESOLVED:
        return (
            f"{left} and {right} are the same amount written in different units; "
            "both resolve to the same value."
        )

    if code == REASON_SAME_VALUE:
        return f"Both report {left} and {right} for {_describe_period(first)}, which agree."

    if code == REASON_VALUE_CONFLICT:
        gap = _relative_gap(first.value_num, second.value_num)
        return (
            f"Same period, scope and measure, but {left} and {right} differ by "
            f"{gap:.1%}, which is outside the agreement band."
        )

    return f"{left} against {right}."


def judge(pair: CandidatePair) -> Verdict:
    """Decide a pair deterministically, from units and context before values."""
    first, second = pair.fact_a, pair.fact_b
    base_confidence = min(first.confidence, second.confidence) * pair.score

    def build(verdict: str, code: str, *, differing: list[str] | None = None) -> Verdict:
        result = Verdict(
            fact_a=first,
            fact_b=second,
            verdict=verdict,
            reason_code=code,
            reason_text="",
            confidence=base_confidence,
            differing_fields=differing or [],
            match_score=pair.score,
        )
        result.reason_text = default_reason_text(result)
        return result

    # 1. The unit gate. An unresolved or incompatible unit can never produce a
    #    contradiction, only an admission that the two cannot be compared.
    if not (first.has_resolved_unit and second.has_resolved_unit):
        return build(VERDICT_NO_VERDICT, REASON_UNIT_MISSING)
    if not is_comparable(first, second):
        return build(VERDICT_NO_VERDICT, REASON_UNIT_INCOMPARABLE)

    # 2. Context. Facts holding under different conditions are reconcilable,
    #    however far apart the numbers are.
    signature_a, signature_b = first.signature, second.signature
    differing = (
        signature_a.differing_fields(signature_b)
        if signature_a and signature_b
        else []
    )

    if differing:
        for field_name in FIELD_PRECEDENCE:
            if field_name not in differing:
                continue
            if field_name == "period":
                period_a, period_b = signature_a.period, signature_b.period
                if period_a.contains(period_b) or period_b.contains(period_a):
                    return build(
                        VERDICT_RECONCILABLE, REASON_PERIOD_SUBSET, differing=differing
                    )
                return build(
                    VERDICT_RECONCILABLE, REASON_PERIOD_DIFF, differing=differing
                )
            if field_name == "scope":
                return build(VERDICT_RECONCILABLE, REASON_SCOPE_DIFF, differing=differing)
            if field_name == "basis":
                return build(VERDICT_RECONCILABLE, REASON_BASIS_DIFF, differing=differing)
            if field_name == "vintage":
                return build(
                    VERDICT_RECONCILABLE, REASON_VINTAGE_REVISION, differing=differing
                )

    # 3. Values, now that the two are known to describe the same thing.
    unit = parse_unit(first.unit, first.value_raw)
    decimals = [d for d in (decimals_of(first.value_raw), decimals_of(second.value_raw)) if d is not None]

    if values_agree(
        first.value_num,
        second.value_num,
        unit,
        decimals=max(decimals) if decimals else None,
    ):
        code = (
            REASON_UNIT_DIFF_RESOLVED
            if _units_written_differently(first, second)
            else REASON_SAME_VALUE
        )
        return build(VERDICT_CORROBORATE, code)

    return build(VERDICT_CONTRADICT, REASON_VALUE_CONFLICT)


def judge_all(pairs: list[CandidatePair]) -> list[Verdict]:
    return [judge(pair) for pair in pairs]


# --- Storage ---------------------------------------------------------------


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(RELATIONSHIP_SCHEMA)


def save_verdicts(
    conn: sqlite3.Connection, collection_id: str, verdicts: list[Verdict]
) -> int:
    """Store relationships, refreshing any that were already recorded."""
    ensure_schema(conn)
    if not verdicts:
        return 0

    rows = []
    for verdict in verdicts:
        first, second = sorted((verdict.fact_a.fact_id, verdict.fact_b.fact_id))
        rows.append(
            (
                verdict.relationship_id,
                collection_id,
                first,
                second,
                verdict.verdict,
                verdict.reason_code,
                verdict.reason_text,
                verdict.confidence,
                verdict.match_score,
                json.dumps(verdict.differing_fields),
                1 if verdict.adjudicated else 0,
                _now(),
            )
        )

    conn.executemany(
        """
        INSERT INTO relationships (
            relationship_id, collection_id, fact_a, fact_b, verdict, reason_code,
            reason_text, confidence, match_score, differing_fields, adjudicated,
            created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(relationship_id) DO UPDATE SET
            verdict          = excluded.verdict,
            reason_code      = excluded.reason_code,
            reason_text      = excluded.reason_text,
            confidence       = excluded.confidence,
            match_score      = excluded.match_score,
            differing_fields = excluded.differing_fields,
            adjudicated      = excluded.adjudicated
        """,
        rows,
    )
    return len(rows)


def list_relationships(
    conn: sqlite3.Connection,
    collection_id: str,
    *,
    verdict: str | None = None,
    reason_code: str | None = None,
    fact_id: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Stored relationships in a collection, most confident first."""
    ensure_schema(conn)

    clauses = ["collection_id = ?"]
    params: list[object] = [collection_id]
    if verdict:
        clauses.append("verdict = ?")
        params.append(verdict)
    if reason_code:
        clauses.append("reason_code = ?")
        params.append(reason_code)
    if fact_id:
        clauses.append("(fact_a = ? OR fact_b = ?)")
        params += [fact_id, fact_id]

    sql = (
        f"SELECT * FROM relationships WHERE {' AND '.join(clauses)} "
        "ORDER BY confidence DESC, relationship_id"
    )
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)

    return [
        {**dict(row), "differing_fields": json.loads(row["differing_fields"] or "[]")}
        for row in conn.execute(sql, params)
    ]


def count_by_verdict(conn: sqlite3.Connection, collection_id: str) -> dict[str, int]:
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT verdict, COUNT(*) AS n FROM relationships "
        "WHERE collection_id = ? GROUP BY verdict",
        (collection_id,),
    )
    return {row["verdict"]: int(row["n"]) for row in rows}


def reconcile(
    conn: sqlite3.Connection,
    collection_id: str,
    *,
    embedder=None,
    client=None,
    new_fact_ids: set[str] | None = None,
    adjudicate_verdicts: bool = True,
    limit: int | None = None,
) -> list[Verdict]:
    """Match, judge, optionally adjudicate, and store - end to end."""
    pairs = find_candidates(
        conn, collection_id, embedder=embedder, new_fact_ids=new_fact_ids, limit=limit
    )
    verdicts = judge_all(pairs)

    if adjudicate_verdicts and verdicts:
        from backend.pipeline.adjudication import adjudicate_all

        adjudicate_all(verdicts, client=client)

    save_verdicts(conn, collection_id, verdicts)
    return verdicts
