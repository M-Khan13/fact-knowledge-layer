"""Reconciliation tests: the deterministic gate, then the adjudicator's limits.

Each case builds two facts as two documents might have written them and checks
which verdict the rules reach. The assertions are about the rules, not about
whether any figure is true.
"""

import json

import pytest

from backend import store
from backend.pipeline.adjudication import (
    Adjudication,
    apply_adjudication,
    adjudicate,
    adjudicate_all,
    build_prompt,
    parse_adjudication,
)
from backend.pipeline.embeddings import HashingEmbedder
from backend.pipeline.matching import CandidatePair
from backend.pipeline.normalization import normalize_fact
from backend.pipeline.reconciliation import (
    REASON_BASIS_DIFF,
    REASON_PERIOD_DIFF,
    REASON_PERIOD_SUBSET,
    REASON_SAME_VALUE,
    REASON_SCOPE_DIFF,
    REASON_UNIT_DIFF_RESOLVED,
    REASON_UNIT_MISSING,
    REASON_VALUE_CONFLICT,
    REASON_VINTAGE_REVISION,
    VERDICT_CONTRADICT,
    VERDICT_CORROBORATE,
    VERDICT_NO_VERDICT,
    VERDICT_RECONCILABLE,
    count_by_verdict,
    judge,
    list_relationships,
    reconcile,
    save_verdicts,
)
from backend.pipeline.schema import Context, Fact, make_fact_id
from tests.conftest import ScriptedClient


def fact(value, unit="₹ Cr", doc="doc_a", attribute="revenue", subject="Acme", **ctx):
    """One fact as a document might have written it."""
    built = Fact(
        fact_id=make_fact_id("c", doc, attribute, str(value), f"{doc}{attribute}{value}"),
        collection_id="c",
        subject=subject,
        subject_key=None,
        attribute=attribute,
        value_raw=str(value),
        value_num=None,
        unit=unit,
        context=Context(**ctx),
        source_doc=doc,
        page=1,
        evidence_span=f"{attribute} of {value} {unit or ''}".strip(),
        confidence=0.9,
    )
    return normalize_fact(built)


def pair_of(first, second, score=1.0) -> CandidatePair:
    return CandidatePair(
        fact_a=first,
        fact_b=second,
        score=score,
        subject_similarity=1.0,
        attribute_similarity=score,
        subject_match="same_key",
        attribute_match="exact",
    )


def verdict_for(first, second, score=1.0):
    return judge(pair_of(first, second, score))


# --- The unit gate comes first --------------------------------------------


def test_an_unresolved_unit_never_contradicts():
    """The hard rule: no unit, no comparison - and never an accusation."""
    result = verdict_for(fact("740", unit=None), fact("8142", doc="doc_b"))

    assert result.verdict == VERDICT_NO_VERDICT
    assert result.reason_code == REASON_UNIT_MISSING


def test_different_unit_families_cannot_be_compared():
    result = verdict_for(fact("6.5", unit="%"), fact("8142", doc="doc_b"))

    assert result.verdict == VERDICT_NO_VERDICT
    assert result.reason_code == REASON_UNIT_MISSING


def test_two_currencies_are_not_converted_into_each_other():
    result = verdict_for(fact("100", unit="₹ bn"), fact("100", unit="US$ bn", doc="doc_b"))

    assert result.verdict == VERDICT_NO_VERDICT


def test_the_unit_gate_wins_even_when_the_context_also_differs():
    """Units are checked first, so an unmeasurable pair never gets a verdict."""
    result = verdict_for(
        fact("740", unit=None, period="FY24"),
        fact("8142", doc="doc_b", period="FY25"),
    )

    assert result.verdict == VERDICT_NO_VERDICT
    assert result.reason_code == REASON_UNIT_MISSING


# --- Context is checked before values --------------------------------------


def test_the_crore_against_million_trap_corroborates():
    """The case this whole design exists to get right."""
    result = verdict_for(
        fact("8,142", unit="₹ Cr", period="FY24"),
        fact("81,415.38", unit="₹ million", period="FY 2023-24", doc="doc_b"),
    )

    assert result.verdict == VERDICT_CORROBORATE
    assert result.reason_code == REASON_UNIT_DIFF_RESOLVED


def test_same_context_and_same_value_corroborates():
    result = verdict_for(
        fact("8,142", period="FY24"), fact("8,142", period="FY24", doc="doc_b")
    )

    assert result.verdict == VERDICT_CORROBORATE
    assert result.reason_code == REASON_SAME_VALUE


def test_same_context_and_different_value_contradicts():
    result = verdict_for(
        fact("8,142", period="FY24"), fact("9,500", period="FY24", doc="doc_b")
    )

    assert result.verdict == VERDICT_CONTRADICT
    assert result.reason_code == REASON_VALUE_CONFLICT


def test_a_quarter_inside_a_year_is_a_part_not_a_conflict():
    result = verdict_for(
        fact("2,076", period="Q4 FY24"), fact("8,142", period="FY24", doc="doc_b")
    )

    assert result.verdict == VERDICT_RECONCILABLE
    assert result.reason_code == REASON_PERIOD_SUBSET
    assert "period" in result.differing_fields


def test_unrelated_periods_are_reconcilable_not_contradictory():
    result = verdict_for(
        fact("8,142", period="FY24"), fact("9,500", period="FY25", doc="doc_b")
    )

    assert result.verdict == VERDICT_RECONCILABLE
    assert result.reason_code == REASON_PERIOD_DIFF


def test_consolidated_against_standalone_is_reconcilable():
    result = verdict_for(
        fact("8,142", period="FY24", scope="Consolidated"),
        fact("7,900", period="FY24", scope="Standalone", doc="doc_b"),
    )

    assert result.verdict == VERDICT_RECONCILABLE
    assert result.reason_code == REASON_SCOPE_DIFF


def test_a_different_measure_is_reconcilable():
    result = verdict_for(
        fact("8,142", period="FY24", basis="revenue from operations"),
        fact("8,900", period="FY24", basis="total income", doc="doc_b"),
    )

    assert result.verdict == VERDICT_RECONCILABLE
    assert result.reason_code == REASON_BASIS_DIFF


def test_a_revision_is_not_a_contradiction():
    result = verdict_for(
        fact("6.4", unit="%", period="FY25", vintage="First Advance Estimate"),
        fact("6.5", unit="%", period="2024-25", vintage="Provisional", doc="doc_b"),
    )

    assert result.verdict == VERDICT_RECONCILABLE
    assert result.reason_code == REASON_VINTAGE_REVISION
    assert "supersedes" in result.reason_text


def test_context_differences_beat_a_value_difference():
    """However far apart the numbers are, a different context is not a conflict."""
    result = verdict_for(
        fact("100", period="FY24", scope="Consolidated"),
        fact("99999", period="FY24", scope="Standalone", doc="doc_b"),
    )

    assert result.verdict == VERDICT_RECONCILABLE


def test_period_takes_precedence_when_several_fields_differ():
    result = verdict_for(
        fact("100", period="Q4 FY24", scope="Consolidated"),
        fact("800", period="FY24", scope="Standalone", doc="doc_b"),
    )

    assert result.reason_code == REASON_PERIOD_SUBSET
    assert result.differing_fields == ["period", "scope"]


# --- Tolerance -------------------------------------------------------------


def test_values_inside_the_band_still_corroborate():
    result = verdict_for(
        fact("1,000", period="FY24"), fact("1,004", period="FY24", doc="doc_b")
    )

    assert result.verdict == VERDICT_CORROBORATE


def test_percentages_use_the_point_band():
    close = verdict_for(
        fact("6.4", unit="%", period="FY24"),
        fact("6.5", unit="%", period="FY24", doc="doc_b"),
    )
    far = verdict_for(
        fact("6.4", unit="%", period="FY24"),
        fact("7.9", unit="%", period="FY24", doc="doc_b"),
    )

    assert close.verdict == VERDICT_CORROBORATE
    assert far.verdict == VERDICT_CONTRADICT


# --- Confidence ------------------------------------------------------------


def test_confidence_reflects_both_facts_and_the_match():
    result = verdict_for(
        fact("8,142", period="FY24"), fact("8,142", period="FY24", doc="doc_b"), score=0.8
    )

    assert result.confidence == pytest.approx(0.9 * 0.8)


def test_an_unresolved_unit_drags_confidence_down():
    """Normalization caps such a fact, and the pair inherits the cap."""
    result = verdict_for(fact("740", unit=None), fact("8,142", doc="doc_b"))

    assert result.confidence <= 0.4


# --- Reason text always exists --------------------------------------------


@pytest.mark.parametrize(
    "first,second",
    [
        (fact("8,142", period="FY24"), fact("8,142", period="FY24", doc="doc_b")),
        (fact("8,142", period="FY24"), fact("9,500", period="FY24", doc="doc_b")),
        (fact("2,076", period="Q4 FY24"), fact("8,142", period="FY24", doc="doc_b")),
        (fact("740", unit=None), fact("8,142", doc="doc_b")),
    ],
)
def test_every_verdict_carries_an_explanation_without_a_model(first, second):
    result = judge(pair_of(first, second))

    assert result.reason_text
    assert not result.adjudicated
    assert len(result.reason_text) > 20


# --- Storage ---------------------------------------------------------------


def test_relationships_persist_and_reload(db):
    store.upsert_collection(db, "c")
    result = verdict_for(
        fact("8,142", period="FY24"), fact("9,500", period="FY24", doc="doc_b")
    )

    save_verdicts(db, "c", [result])
    rows = list_relationships(db, "c")

    assert len(rows) == 1
    assert rows[0]["verdict"] == VERDICT_CONTRADICT
    assert rows[0]["reason_code"] == REASON_VALUE_CONFLICT
    assert rows[0]["reason_text"] == result.reason_text
    assert rows[0]["differing_fields"] == []


def test_saving_the_same_pair_twice_updates_in_place(db):
    store.upsert_collection(db, "c")
    result = verdict_for(fact("8,142", period="FY24"), fact("8,142", period="FY24", doc="doc_b"))

    save_verdicts(db, "c", [result])
    result.reason_text = "a better sentence"
    save_verdicts(db, "c", [result])

    rows = list_relationships(db, "c")
    assert len(rows) == 1
    assert rows[0]["reason_text"] == "a better sentence"


def test_relationships_filter_by_verdict_and_fact(db):
    store.upsert_collection(db, "c")
    agreeing = verdict_for(fact("8,142", period="FY24"), fact("8,142", period="FY24", doc="doc_b"))
    conflicting = verdict_for(
        fact("100", period="FY24", attribute="margin"),
        fact("900", period="FY24", attribute="margin", doc="doc_b"),
    )
    save_verdicts(db, "c", [agreeing, conflicting])

    assert len(list_relationships(db, "c", verdict=VERDICT_CORROBORATE)) == 1
    assert len(list_relationships(db, "c", verdict=VERDICT_CONTRADICT)) == 1
    assert len(list_relationships(db, "c", fact_id=agreeing.fact_a.fact_id)) == 1
    assert count_by_verdict(db, "c") == {VERDICT_CORROBORATE: 1, VERDICT_CONTRADICT: 1}


# --- The adjudicator's limits ---------------------------------------------


def test_the_model_may_rewrite_the_sentence():
    result = verdict_for(fact("8,142", period="FY24"), fact("8,142", period="FY24", doc="doc_b"))
    original = result.reason_text

    accepted = apply_adjudication(result, {"reason_text": "Both documents report the same figure."})

    assert accepted
    assert result.reason_text == "Both documents report the same figure."
    assert result.reason_text != original
    assert result.adjudicated


def test_the_model_may_lower_confidence_but_never_raise_it():
    result = verdict_for(fact("8,142", period="FY24"), fact("8,142", period="FY24", doc="doc_b"))
    ceiling = result.confidence

    apply_adjudication(result, {"reason_text": "ok", "confidence": 0.99})
    assert result.confidence == ceiling, "a higher confidence must be ignored"

    apply_adjudication(result, {"reason_text": "ok", "confidence": 0.3})
    assert result.confidence == pytest.approx(0.3)


def test_the_model_cannot_overturn_a_verdict():
    """The whole guardrail: the rules decide, the model explains."""
    result = verdict_for(fact("8,142", period="FY24"), fact("9,500", period="FY24", doc="doc_b"))
    original_text = result.reason_text

    accepted = apply_adjudication(
        result,
        {"verdict": VERDICT_CORROBORATE, "reason_text": "Actually these agree."},
    )

    assert not accepted
    assert result.verdict == VERDICT_CONTRADICT
    assert result.reason_text == original_text


def test_a_verdict_echoed_back_unchanged_is_accepted():
    result = verdict_for(fact("8,142", period="FY24"), fact("9,500", period="FY24", doc="doc_b"))

    accepted = apply_adjudication(
        result, {"verdict": VERDICT_CONTRADICT, "reason_text": "The two figures differ."}
    )

    assert accepted
    assert result.reason_text == "The two figures differ."


def test_a_rambling_reason_is_trimmed():
    result = verdict_for(fact("8,142", period="FY24"), fact("8,142", period="FY24", doc="doc_b"))

    apply_adjudication(result, {"reason_text": "word " * 400})

    assert len(result.reason_text) <= 300


def test_a_nonsense_confidence_is_ignored():
    result = verdict_for(fact("8,142", period="FY24"), fact("8,142", period="FY24", doc="doc_b"))
    ceiling = result.confidence

    apply_adjudication(result, {"reason_text": "ok", "confidence": "not a number"})

    assert result.confidence == ceiling


def test_parse_adjudication_tolerates_fences_and_prose():
    assert parse_adjudication('```json\n{"reason_text": "x"}\n```')["reason_text"] == "x"
    assert parse_adjudication('Sure: {"reason_text": "x"}')["reason_text"] == "x"
    assert parse_adjudication('[{"reason_text": "x"}]')["reason_text"] == "x"


def test_a_failing_model_leaves_the_rules_explanation():
    result = verdict_for(fact("8,142", period="FY24"), fact("9,500", period="FY24", doc="doc_b"))
    original = result.reason_text
    client = ScriptedClient(["not json at all"])

    accepted = adjudicate(result, client, sleep=lambda _s: None)

    assert not accepted
    assert result.reason_text == original
    assert result.verdict == VERDICT_CONTRADICT


def test_adjudication_is_skipped_entirely_without_a_key(monkeypatch):
    monkeypatch.setattr("backend.config.GEMINI_API_KEY", "")
    result = verdict_for(fact("8,142", period="FY24"), fact("8,142", period="FY24", doc="doc_b"))
    original = result.reason_text

    assert adjudicate_all([result]) == 0
    assert result.reason_text == original


def test_the_prompt_carries_both_evidence_spans_and_the_decision():
    result = verdict_for(
        fact("8,142", period="FY24"), fact("9,500", period="FY24", doc="doc_b")
    )

    prompt = build_prompt(result)

    assert result.fact_a.evidence_span in prompt
    assert result.fact_b.evidence_span in prompt
    assert VERDICT_CONTRADICT in prompt
    assert "do not change" in prompt.lower()


# --- End to end ------------------------------------------------------------


def test_reconcile_matches_judges_and_stores(db):
    store.upsert_collection(db, "c")
    facts = [
        fact("8,142", period="FY24", doc="doc_a"),
        fact("81,415.38", unit="₹ million", period="FY 2023-24", doc="doc_b"),
    ]
    for item in facts:
        item.subject_key = "name:acme"
    store.save_facts(db, facts)

    verdicts = reconcile(
        db, "c", embedder=HashingEmbedder(), adjudicate_verdicts=False
    )

    assert len(verdicts) == 1
    assert verdicts[0].verdict == VERDICT_CORROBORATE
    assert len(list_relationships(db, "c")) == 1


def test_reconcile_uses_the_adjudicator_when_given_a_client(db):
    store.upsert_collection(db, "c")
    facts = [fact("8,142", period="FY24", doc="doc_a"), fact("8,142", period="FY24", doc="doc_b")]
    for item in facts:
        item.subject_key = "name:acme"
    store.save_facts(db, facts)

    client = ScriptedClient([json.dumps({"reason_text": "Both documents agree."})])
    verdicts = reconcile(db, "c", embedder=HashingEmbedder(), client=client)

    assert verdicts[0].reason_text == "Both documents agree."
    assert list_relationships(db, "c")[0]["adjudicated"] == 1


def test_the_part_of_whole_sentence_names_the_whole_correctly():
    """The containing period must be described as the one doing the covering."""
    quarter = fact("2,076", period="Q4 FY24", doc="doc_a")
    year = fact("8,142", period="FY24", doc="doc_b")

    both_ways = [judge(pair_of(quarter, year)), judge(pair_of(year, quarter))]

    for result in both_ways:
        assert result.reason_code == REASON_PERIOD_SUBSET
        text = result.reason_text
        assert text.startswith("FY2024 covers Q4 FY2024"), text
        assert "2,076 ₹ Cr is a part of 8,142 ₹ Cr" in text, text
