"""Numbers and units.

Two rules drive this module:

* A value is stored in a single base unit alongside the unit it was written in,
  so "8,142 Cr" and "81,415 million" become the same number.
* Unit-blind comparison is banned. A value whose unit cannot be resolved gets
  no unit at all rather than a guessed one, and unresolved values are never
  compared. Units also carry a *family*, and families never cross-compare, so a
  percentage can never contradict a headcount.

Unrecognised units are not an error. A unit this code has never seen becomes a
canonical token of its own, so an unfamiliar document compares correctly with
itself instead of being forced into a fixed vocabulary.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Magnitude words scale a value within whatever family it belongs to.
MAGNITUDES: dict[str, float] = {
    "hundred": 1e2,
    "thousand": 1e3,
    "k": 1e3,
    "lakh": 1e5,
    "lac": 1e5,
    "lakhs": 1e5,
    "million": 1e6,
    "mn": 1e6,
    "mil": 1e6,
    "m": 1e6,
    "crore": 1e7,
    "cr": 1e7,
    "crores": 1e7,
    "billion": 1e9,
    "bn": 1e9,
    "b": 1e9,
    "trillion": 1e12,
    "tn": 1e12,
    "tr": 1e12,
}

# Currency written as a symbol or an ISO-ish code. The canonical unit is the code.
CURRENCY_SYMBOLS: dict[str, str] = {
    "₹": "INR",  # rupee
    "rs": "INR",
    "rs.": "INR",
    "inr": "INR",
    "रू": "INR",
    "$": "USD",
    "us$": "USD",
    "usd": "USD",
    "€": "EUR",
    "eur": "EUR",
    "£": "GBP",
    "gbp": "GBP",
    "¥": "JPY",
    "jpy": "JPY",
}

PERCENT_MARKERS = ("%", "percent", "per cent", "percentage")
POINT_MARKERS = ("percentage point", "percentage points", "pp", "ppt", "bps", "basis point")
RATIO_MARKERS = ("ratio", "times", "x")

# Families never compare across. Keeping them explicit is what enforces the ban.
FAMILY_CURRENCY = "currency"
FAMILY_PERCENT = "percent"
FAMILY_POINT = "percentage_point"
FAMILY_RATIO = "ratio"
FAMILY_OTHER = "other"

_NUMBER_RE = re.compile(r"[-+]?\d(?:[\d,\s]*\d)?(?:\.\d+)?")


@dataclass(frozen=True)
class ResolvedUnit:
    """A unit reduced to something comparable.

    ``canonical`` is the base unit a value is expressed in after scaling;
    ``scale`` is what the written value must be multiplied by to reach it.
    """

    family: str
    canonical: str
    scale: float = 1.0
    raw: str | None = None

    def comparable_with(self, other: ResolvedUnit | None) -> bool:
        """Same family and same base unit, or the two must not be compared."""
        if other is None:
            return False
        return self.family == other.family and self.canonical == other.canonical


def _clean(text: str) -> str:
    return unicodedata.normalize("NFKC", text).replace("−", "-").strip()


def parse_number(value: str | None) -> float | None:
    """Read the number out of a written value.

    Returns None when there is no number, or more than one, since a value like
    "5 to 6" has no single meaning and guessing which end to use would be
    inventing data. Digit grouping is stripped, so both Western (1,234,567) and
    Indian (12,34,567) grouping work.
    """
    if not value:
        return None

    text = _clean(value)
    if not text:
        return None

    negative = False
    # Accounting notation: a figure in parentheses is negative.
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()

    matches = [m.group(0) for m in _NUMBER_RE.finditer(text)]
    cleaned = []
    for match in matches:
        stripped = match.replace(",", "").replace(" ", "").replace("\t", "")
        if stripped in {"", "-", "+"}:
            continue
        cleaned.append(stripped)

    if len(cleaned) != 1:
        return None

    try:
        number = float(cleaned[0])
    except ValueError:
        return None

    return -number if negative else number


def _find_magnitude(text: str) -> tuple[float, str | None]:
    """Largest magnitude word present, as a whole word."""
    best_scale, best_word = 1.0, None
    for word, scale in MAGNITUDES.items():
        if re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text):
            if scale > best_scale:
                best_scale, best_word = scale, word
    return best_scale, best_word


def _find_currency(text: str) -> str | None:
    for token, code in CURRENCY_SYMBOLS.items():
        if token.isalpha():
            if re.search(rf"(?<![a-z]){re.escape(token)}(?![a-z])", text):
                return code
        elif token in text:
            return code
    return None


def _slug(text: str) -> str:
    """A canonical token for a unit nobody anticipated."""
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug or "unit"


def _strip_noise(text: str) -> str:
    """Remove numbers, currency and magnitude words, leaving the unit itself."""
    without_numbers = _NUMBER_RE.sub(" ", text)
    tokens = [
        token
        for token in re.split(r"[\s/,()\[\]]+", without_numbers)
        if token
        and token not in CURRENCY_SYMBOLS
        and token not in MAGNITUDES
        and not all(ch in "₹$€£¥.-" for ch in token)
    ]
    return " ".join(tokens).strip()


def parse_unit(unit_text: str | None, value_raw: str | None = None) -> ResolvedUnit | None:
    """Resolve a unit from what was written, or return None if it cannot be.

    Both the unit field and the value string are inspected, because a document
    may put the unit in either ("unit: ₹ million", or "₹1,266 Cr" inline).
    Returning None is a real answer: it marks the value as not comparable.
    """
    parts = [p for p in (unit_text, value_raw) if p]
    if not parts:
        return None

    haystack = _clean(" ".join(parts)).lower()
    written = _clean(unit_text) if unit_text else None

    scale, _magnitude_word = _find_magnitude(haystack)

    # Percentage points before percent: "percentage point" contains "percentage".
    if any(marker in haystack for marker in POINT_MARKERS):
        point_scale = 0.01 if ("bps" in haystack or "basis point" in haystack) else 1.0
        return ResolvedUnit(FAMILY_POINT, "percentage_point", point_scale, written)

    if any(marker in haystack for marker in PERCENT_MARKERS):
        return ResolvedUnit(FAMILY_PERCENT, "percent", 1.0, written)

    currency = _find_currency(haystack)
    if currency:
        return ResolvedUnit(FAMILY_CURRENCY, currency, scale, written)

    remainder = _strip_noise(haystack)

    if remainder in RATIO_MARKERS or remainder in {"x", "times"}:
        return ResolvedUnit(FAMILY_RATIO, "ratio", 1.0, written)

    if remainder:
        return ResolvedUnit(FAMILY_OTHER, _slug(remainder), scale, written)

    # A bare magnitude with no dimension ("740 Mn" of what?) stays unresolved
    # rather than being assumed to be a count.
    return None


def to_base(value_raw: str | None, unit: ResolvedUnit | None) -> float | None:
    """The value expressed in its unit's base, or None if it cannot be resolved."""
    number = parse_number(value_raw)
    if number is None or unit is None:
        return None
    return number * unit.scale
