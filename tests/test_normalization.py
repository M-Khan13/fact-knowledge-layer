"""Tests for the normalization rules, one section per rule.

These are pure functions over written text, so nothing here needs a document
or a model. The inputs are notations a report might use; the assertions are
about how those notations reduce, never about whether a figure is true.
"""

import pytest

from backend.pipeline import entities
from backend.pipeline.normalization import (
    CONSOLIDATED,
    STANDALONE,
    UNRESOLVED_UNIT_CONFIDENCE,
    VINTAGE_ADVANCE,
    VINTAGE_FINAL,
    VINTAGE_PROJECTION,
    VINTAGE_PROVISIONAL,
    VINTAGE_REVISED,
    build_signature,
    canonical_token,
    decimals_of,
    is_comparable,
    newer_vintage,
    normalize_basis,
    normalize_fact,
    normalize_facts,
    normalize_scope,
    normalize_vintage,
    values_agree,
)
from backend.pipeline.schema import Context, Fact
from backend.pipeline.temporal import parse_period
from backend.pipeline.units import parse_number, parse_unit, to_base


def make_fact(**overrides) -> Fact:
    """A minimal fact for exercising normalization."""
    base = dict(
        fact_id="f_test",
        collection_id="c",
        subject="Some Entity",
        subject_key=None,
        attribute="some_measure",
        value_raw="1",
        value_num=None,
        unit=None,
        context=Context(),
        source_doc="doc",
        page=1,
        evidence_span="evidence",
        confidence=0.9,
    )
    context_fields = {k: overrides.pop(k) for k in ("period", "scope", "basis", "vintage") if k in overrides}
    base.update(overrides)
    if context_fields:
        base["context"] = Context(**context_fields)
    return Fact(**base)


# --- 3a. Units -------------------------------------------------------------


def test_numbers_survive_western_and_indian_grouping():
    assert parse_number("81,415.38") == 81415.38
    assert parse_number("12,34,567") == 1234567  # lakh grouping
    assert parse_number("1,234,567") == 1234567
    assert parse_number("(1,234)") == -1234  # accounting negative


def test_ambiguous_numbers_are_refused():
    """A range has no single value; guessing an end would invent data."""
    assert parse_number("5 to 6") is None
    assert parse_number("no digits here") is None
    assert parse_number(None) is None


def test_magnitudes_reduce_to_one_base():
    """The trap this exists to kill: crore against million."""
    in_crore = to_base("8,142", parse_unit("₹ Cr"))
    in_million = to_base("81,415.38", parse_unit("₹ million"))

    assert in_crore == pytest.approx(8.142e10)
    assert in_million == pytest.approx(8.141538e10)
    assert values_agree(in_crore, in_million), "same money, different notation"


@pytest.mark.parametrize(
    "unit_text,expected",
    [
        ("₹ Cr", 1e7),
        ("INR crore", 1e7),
        ("₹ lakh", 1e5),
        ("₹ million", 1e6),
        ("Rs. mn", 1e6),
        ("₹ bn", 1e9),
    ],
)
def test_currency_magnitudes(unit_text, expected):
    resolved = parse_unit(unit_text)

    assert resolved.family == "currency"
    assert resolved.canonical == "INR"
    assert resolved.scale == expected


def test_unit_can_come_from_the_value_itself():
    resolved = parse_unit(None, "₹1,266 Cr")

    assert resolved.canonical == "INR"
    assert to_base("₹1,266 Cr", resolved) == pytest.approx(1.266e10)


def test_a_value_with_no_unit_stays_unresolved():
    """A bare number is not assumed to be a count, or anything else."""
    assert parse_unit(None, "740") is None
    assert parse_unit(None, None) is None


def test_unfamiliar_units_get_their_own_token():
    """A dynamic schema: an unseen unit must still compare with itself."""
    shipments = parse_unit("shipments", "740")
    megawatts = parse_unit("MW", "12")

    assert shipments.canonical == "shipments"
    assert megawatts.canonical == "mw"
    assert not shipments.comparable_with(megawatts)
    assert shipments.comparable_with(parse_unit("shipments", "900"))


def test_families_never_cross_compare():
    percent = parse_unit("%")
    rupees = parse_unit("₹ Cr")
    points = parse_unit("percentage points")

    assert not percent.comparable_with(rupees)
    assert not percent.comparable_with(points)
    assert not rupees.comparable_with(None)


def test_currencies_do_not_convert_into_each_other():
    """No exchange rate is invented, so the two simply cannot be compared."""
    assert not parse_unit("₹ bn").comparable_with(parse_unit("US$ bn"))


def test_basis_points_scale_into_percentage_points():
    assert to_base("50", parse_unit("bps")) == pytest.approx(0.5)


# --- 3b. Temporal ----------------------------------------------------------


@pytest.mark.parametrize(
    "written,fiscal_year",
    [
        ("FY24", 2024),
        ("FY 24", 2024),
        ("FY2024", 2024),
        ("FY 2023-24", 2024),
        ("2023-24", 2024),
        ("FY2024/25", 2025),
        ("2024-25", 2025),
        ("2021/22", 2022),
        ("FY25", 2025),
    ],
)
def test_fiscal_years_are_named_for_the_year_they_end(written, fiscal_year):
    assert parse_period(written).fiscal_year == fiscal_year


def test_different_publishers_agree_on_one_token():
    """The whole point: three notations, one period."""
    assert (
        parse_period("FY2024/25").signature()
        == parse_period("2024-25").signature()
        == parse_period("FY25").signature()
    )


def test_year_ended_phrasing_resolves_to_a_fiscal_year():
    period = parse_period("for the year ended March 31, 2024")

    assert period.period_type == "FY"
    assert period.fiscal_year == 2024


def test_a_day_is_never_mistaken_for_a_year():
    assert parse_period("as at March 31, 2024").fiscal_year == 2024
    assert parse_period("as at 31 March 2024").fiscal_year == 2024


def test_quarter_and_half_markers():
    quarter = parse_period("Q4 FY24")
    half = parse_period("H1 FY25")

    assert (quarter.period_type, quarter.fiscal_year, quarter.sub_period) == ("Q", 2024, "Q4")
    assert (half.period_type, half.fiscal_year, half.sub_period) == ("H", 2025, "H1")


def test_closing_dates_map_onto_fiscal_quarters():
    """December closes the third quarter of an April-March year."""
    quarter = parse_period("quarter ended December 31, 2023")

    assert (quarter.period_type, quarter.fiscal_year, quarter.sub_period) == ("Q", 2024, "Q3")


def test_unparseable_periods_stay_unresolved():
    for text in ("", None, "the period", "sometime soon"):
        assert not parse_period(text).is_resolved


def test_subset_logic_recognises_part_of_whole():
    year, quarter = parse_period("FY24"), parse_period("Q4 FY24")

    assert year.contains(quarter)
    assert not quarter.contains(year)


def test_halves_contain_their_quarters():
    assert parse_period("H1 FY25").contains(parse_period("Q1 FY25"))
    assert not parse_period("H1 FY25").contains(parse_period("Q4 FY25"))


def test_a_period_does_not_contain_itself():
    """Containment means part-of-whole, which an identical period is not."""
    assert not parse_period("FY24").contains(parse_period("FY24"))


def test_containment_requires_the_same_fiscal_year():
    assert not parse_period("FY24").contains(parse_period("Q4 FY25"))


# --- 3c. Scope and basis ---------------------------------------------------


def test_scope_resolves_to_consolidated_or_standalone():
    assert normalize_scope("Consolidated") == CONSOLIDATED
    assert normalize_scope("consolidated financial statements") == CONSOLIDATED
    assert normalize_scope("Standalone") == STANDALONE
    assert normalize_scope("stand-alone") == STANDALONE
    assert normalize_scope("unconsolidated") == STANDALONE
    assert normalize_scope(None) is None


def test_basis_is_canonical_but_not_constrained():
    """Free text, so an unfamiliar basis is a new token rather than a failure."""
    assert normalize_basis("GDP (at market prices)") == normalize_basis("GDP at market prices")
    assert normalize_basis("Revenue from operations") == "revenue_from_operations"
    assert normalize_basis("some measure nobody has seen") == "some_measure_nobody_has_seen"
    assert normalize_basis("GVA") != normalize_basis("GDP")


def test_canonical_token_drops_filler_not_meaning():
    assert canonical_token("real") != canonical_token("nominal")
    assert canonical_token("  Total Income  ") == "total_income"
    assert canonical_token(None) is None


# --- 3d. Vintage -----------------------------------------------------------


@pytest.mark.parametrize(
    "written,expected",
    [
        ("First Advance Estimate", VINTAGE_ADVANCE),
        ("advance estimates", VINTAGE_ADVANCE),
        ("Provisional", VINTAGE_PROVISIONAL),
        ("Revised Estimate", VINTAGE_REVISED),
        ("Final", VINTAGE_FINAL),
        ("actuals", VINTAGE_FINAL),
        ("Projection", VINTAGE_PROJECTION),
        ("forecast", VINTAGE_PROJECTION),
    ],
)
def test_vintage_is_recognised(written, expected):
    assert normalize_vintage(written) == expected


def test_advance_estimate_is_not_read_as_a_plain_estimate():
    """Order matters: 'advance estimate' must not fall through to something else."""
    assert normalize_vintage("first advance estimate") == VINTAGE_ADVANCE


def test_revision_precedence():
    assert newer_vintage(VINTAGE_ADVANCE, VINTAGE_PROVISIONAL) == VINTAGE_PROVISIONAL
    assert newer_vintage(VINTAGE_REVISED, VINTAGE_FINAL) == VINTAGE_FINAL
    assert newer_vintage(VINTAGE_ADVANCE, VINTAGE_FINAL) == VINTAGE_FINAL
    assert newer_vintage(VINTAGE_FINAL, VINTAGE_FINAL) is None
    assert newer_vintage(VINTAGE_PROJECTION, VINTAGE_FINAL) is None  # not a revision
    assert newer_vintage(None, VINTAGE_FINAL) is None


# --- 3e. Entities ----------------------------------------------------------


def test_a_stated_identifier_beats_a_name():
    """Two spellings of one director resolve to the same key."""
    full = entities.resolve_subject_key("Sahil Barua", "DIN: 01344131")
    short = entities.resolve_subject_key("S. Barua", "DIN 01344131")

    assert full == short == "DIN:01344131"


def test_company_identifiers_are_matched_by_shape_not_by_a_list():
    key = entities.resolve_subject_key("Some Company", "CIN L63090DL2011PLC221234")

    assert key == "CIN:L63090DL2011PLC221234"


def test_identifiers_are_read_out_of_the_evidence_too():
    key = entities.resolve_subject_key(
        "A Director", None, "appointed as director, DIN 01234567, with effect from"
    )

    assert key == "DIN:01234567"


def test_legal_suffixes_do_not_split_one_company_in_two():
    assert entities.normalize_name("Acme Logistics Limited") == entities.normalize_name(
        "acme logistics ltd."
    )
    assert entities.normalize_name("Mr. Sahil Barua") == "sahil barua"


def test_two_different_companies_stay_different():
    assert entities.normalize_name("Acme Logistics") != entities.normalize_name("Acme Freight")


def test_addresses_normalise_whitespace_case_and_abbreviations():
    assert entities.normalize_address("12, MG Road,  Bengaluru") == entities.normalize_address(
        "12 MG ROAD Bengaluru"
    )
    assert entities.normalize_address("Opposite Sector 5") == entities.normalize_address(
        "opp. sec 5"
    )


def test_different_addresses_stay_different():
    assert entities.normalize_address("12 MG Road") != entities.normalize_address("13 MG Road")


def test_self_reference_detection():
    assert entities.is_self_reference("the Company")
    assert entities.is_self_reference("your Company")
    assert not entities.is_self_reference("Acme Logistics Limited")


def test_self_references_bind_to_the_document_they_appear_in():
    """'the Company' means whoever that document is about, worked out at runtime."""
    facts = [
        make_fact(fact_id="f1", subject="Acme Logistics Limited", source_doc="a"),
        make_fact(fact_id="f2", subject="the Company", source_doc="a"),
        make_fact(fact_id="f3", subject="Other Corp", source_doc="b"),
        make_fact(fact_id="f4", subject="the Company", source_doc="b"),
    ]

    normalize_facts(facts)
    keys = {fact.fact_id: fact.subject_key for fact in facts}

    assert keys["f2"] == keys["f1"]
    assert keys["f4"] == keys["f3"]
    assert keys["f2"] != keys["f4"], "each document binds to its own entity"


# --- 3f. Tolerance ---------------------------------------------------------


def test_values_inside_the_relative_band_agree():
    assert values_agree(1000.0, 1004.0)  # 0.4%
    assert not values_agree(1000.0, 1010.0)  # 1.0%


def test_the_band_widens_to_the_last_reported_decimal():
    """A figure quoted to fewer decimals must not be forced to disagree."""
    assert values_agree(6.0, 6.4, decimals=0)
    assert not values_agree(6.0, 6.4, decimals=2)


def test_percentages_use_a_point_based_band():
    percent = parse_unit("%")

    assert values_agree(6.4, 6.5, percent)
    assert not values_agree(6.4, 6.8, percent)


def test_missing_values_never_agree():
    assert not values_agree(None, 1.0)
    assert not values_agree(1.0, None)


def test_decimals_of():
    assert decimals_of("81,415.38") == 2
    assert decimals_of("6") == 0
    assert decimals_of(None) is None


# --- Applying it to a fact -------------------------------------------------


def test_normalizing_a_fact_populates_value_and_signature():
    fact = make_fact(
        value_raw="8,142", unit="₹ Cr", period="FY24", scope="Consolidated",
        basis="Revenue from operations", vintage="Audited",
    )

    normalize_fact(fact)

    assert fact.value_num == pytest.approx(8.142e10)
    assert fact.unit_canonical == "INR"
    assert fact.unit_raw == "₹ Cr"
    assert fact.signature.period.fiscal_year == 2024
    assert fact.signature.scope == CONSOLIDATED
    assert fact.signature.vintage == VINTAGE_FINAL
    assert fact.normalized


def test_an_unresolved_unit_caps_confidence_and_blocks_comparison():
    """The unit-blind ban: no unit means no comparison, and low confidence."""
    fact = make_fact(value_raw="740", unit=None, confidence=0.95)

    normalize_fact(fact)

    assert fact.value_num is None
    assert fact.unit_canonical is None
    assert fact.confidence <= UNRESOLVED_UNIT_CONFIDENCE
    assert not fact.has_resolved_unit


def test_comparability_requires_both_sides_to_be_resolved():
    resolved = normalize_fact(make_fact(value_raw="100", unit="₹ Cr"))
    unresolved = normalize_fact(make_fact(fact_id="f2", value_raw="100", unit=None))
    percentage = normalize_fact(make_fact(fact_id="f3", value_raw="100", unit="%"))

    assert is_comparable(resolved, normalize_fact(make_fact(fact_id="f4", value_raw="1", unit="₹ mn")))
    assert not is_comparable(resolved, unresolved)
    assert not is_comparable(resolved, percentage)


def test_signatures_report_which_field_differs():
    consolidated = build_signature("FY24", "Consolidated", "revenue", None)
    standalone = build_signature("FY24", "Standalone", "revenue", None)
    quarterly = build_signature("Q4 FY24", "Consolidated", "revenue", None)

    assert consolidated.matches(build_signature("FY 2023-24", "consolidated", "revenue", None))
    assert consolidated.differing_fields(standalone) == ["scope"]
    assert consolidated.differing_fields(quarterly) == ["period"]


def test_a_bracketed_negative_survives_a_trailing_unit():
    """"(452) Cr" is a loss of 452 crore. Losing the sign misstates the figure."""
    assert parse_number("(452)") == -452
    assert parse_number("(452) Cr") == -452
    assert parse_number("(1,234.5) mn") == -1234.5
    assert parse_number("(6.3%)") == -6.3
    assert parse_number("₹(452) Cr") == -452
    # Unbracketed figures are untouched.
    assert parse_number("452 Cr") == 452
    assert parse_number("8,142") == 8142
