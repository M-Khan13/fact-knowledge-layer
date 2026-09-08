"""Comparing measurement bases without mistaking two axes for one.

A `basis` string often names only part of how a figure is measured. One
document writes "real", naming the price basis and saying nothing about which
aggregate. Another writes "GDP at market prices", naming the aggregate and
saying nothing about prices. Compared as opaque strings those look like a
disagreement, and a corroboration turns into a spurious "different basis".

They are not in disagreement: they describe different axes. So a basis is read
as a set of *facets*, and two bases conflict only where they state different
values for the same facet. Saying nothing about an axis is not a contradiction
of what the other document says about it.

The vocabulary below is general measurement wording, not anything tied to a
document. A basis using words this module does not recognise still compares
exactly as it did before - by its canonical token - so an unfamiliar basis is
never quietly unified with another.
"""

from __future__ import annotations

import re

PRICE_BASIS = "price_basis"
AGGREGATE = "aggregate"
ADJUSTMENT = "adjustment"

# Each entry maps a pattern to the facet it fixes and the value it fixes it to.
# Ordered: the first match on a facet wins, so more specific wording goes first.
FACET_PATTERNS: tuple[tuple[str, str, str], ...] = (
    # How prices are treated.
    (r"\bconstant[_\s-]?price", PRICE_BASIS, "real"),
    (r"\binflation[_\s-]?adjusted\b", PRICE_BASIS, "real"),
    (r"\breal\b", PRICE_BASIS, "real"),
    (r"\bcurrent[_\s-]?price", PRICE_BASIS, "nominal"),
    (r"\bnominal\b", PRICE_BASIS, "nominal"),
    # Which aggregate is being measured.
    (r"\bgva\b|\bgross[_\s-]?value[_\s-]?added\b", AGGREGATE, "gva"),
    (r"\bfactor[_\s-]?cost\b", AGGREGATE, "gdp_factor_cost"),
    (r"\bmarket[_\s-]?price", AGGREGATE, "gdp_market_price"),
    (r"\bgni\b|\bgross[_\s-]?national[_\s-]?income\b", AGGREGATE, "gni"),
    (r"\bgdp\b", AGGREGATE, "gdp"),
    # Whether a series has been adjusted.
    (r"\bseasonally[_\s-]?adjusted\b", ADJUSTMENT, "seasonally_adjusted"),
    (r"\bnot[_\s-]?seasonally[_\s-]?adjusted\b", ADJUSTMENT, "unadjusted"),
)


def facets(basis: str | None) -> dict[str, str]:
    """Which measurement axes a basis actually pins down.

    An empty result means the wording is not one this module knows how to break
    apart, which is a signal to fall back to comparing the whole token.
    """
    if not basis or not basis.strip():
        return {}

    # Underscores and hyphens are word characters, so "gdp_market_price" has no
    # word boundary before "market". Flatten them to spaces before matching.
    text = re.sub(r"[_\-]+", " ", basis.lower())
    found: dict[str, str] = {}
    for pattern, facet, value in FACET_PATTERNS:
        if facet in found:
            continue
        if re.search(pattern, text):
            found[facet] = value
    return found


def conflict(first: str | None, second: str | None) -> bool:
    """Whether two bases genuinely disagree about how something was measured.

    Identical bases never conflict. Where both are understood, they conflict
    only on an axis they both name and disagree about - so "real" and "GDP at
    market prices" agree, while "real" and "nominal" do not. Where either is
    unrecognised wording, the comparison stays strict and any difference
    counts, so an unfamiliar basis is never unified with something else.
    """
    if first == second:
        return False
    if first is None or second is None:
        # Handled by the caller: absence is a different question from disagreement.
        return first != second

    left, right = facets(first), facets(second)
    if not left or not right:
        return True

    shared = left.keys() & right.keys()
    if shared:
        return any(left[axis] != right[axis] for axis in shared)

    # Both understood, and they pin down different axes entirely.
    return False
