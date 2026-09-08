"""Comparing measurement bases.

A basis often names only one axis of how a figure was measured. Two documents
naming different axes are not disagreeing, and treating them as though they
were turns a corroboration into a spurious "different basis".
"""

import pytest

from backend.pipeline.basis import AGGREGATE, PRICE_BASIS, conflict, facets
from backend.pipeline.normalization import build_signature


def test_a_basis_is_read_as_the_axes_it_pins_down():
    assert facets("real") == {PRICE_BASIS: "real"}
    assert facets("GDP_market_price") == {AGGREGATE: "gdp_market_price"}
    assert facets("nominal") == {PRICE_BASIS: "nominal"}
    assert facets("GVA") == {AGGREGATE: "gva"}
    assert facets(None) == {}
    assert facets("something nobody has written before") == {}


def test_different_axes_do_not_conflict():
    """The case this exists for: one names prices, the other names the aggregate."""
    assert not conflict("real", "GDP_market_price")
    assert not conflict("GDP_market_price", "real")


def test_the_same_axis_stated_differently_still_conflicts():
    assert conflict("real", "nominal")
    assert conflict("GVA", "GDP_market_price")
    assert conflict("constant prices", "current prices")


def test_wording_variants_land_on_the_same_axis_value():
    assert not conflict("real", "at constant prices")
    assert not conflict("nominal", "at current prices")
    assert conflict("at constant prices", "nominal")


def test_a_basis_naming_both_axes_conflicts_on_either():
    combined = "real GDP at market prices"

    assert facets(combined) == {PRICE_BASIS: "real", AGGREGATE: "gdp_market_price"}
    assert not conflict(combined, "real")
    assert not conflict(combined, "GDP_market_price")
    assert conflict(combined, "nominal")
    assert conflict(combined, "GVA")


def test_identical_bases_never_conflict():
    assert not conflict("real", "real")
    assert not conflict("whatever this is", "whatever this is")


def test_unrecognised_wording_stays_strict():
    """An unfamiliar basis must never be quietly unified with another."""
    assert conflict("some bespoke measure", "another bespoke measure")
    assert conflict("real", "some bespoke measure")


def test_signatures_stop_reporting_a_basis_difference():
    real = build_signature("FY2024/25", None, "real", None)
    market = build_signature("2024/25", None, "GDP_market_price", None)
    nominal = build_signature("FY2024/25", None, "nominal", None)

    assert real.differing_fields(market) == []
    assert real.matches(market), "the same figure, described along different axes"
    assert real.differing_fields(nominal) == ["basis"]
