"""Periods, Indian fiscal years, and the subset relation between them.

An Indian fiscal year runs 1 April to 31 March and is named for the calendar
year it *ends* in: FY24 is 1 Apr 2023 - 31 Mar 2024. That single convention is
what lets different publishers line up - "FY2024/25", "2024-25" and "FY25" all
describe one period and all resolve to fiscal_year 2025.

Periods also nest. Q4 FY24 sits inside FY24, and H1 FY25 inside FY25. A value
difference across a containment boundary is a part against a whole, not a
disagreement, so `contains()` exists to let the verdict engine say so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

FY = "FY"
HALF = "H"
QUARTER = "Q"
CY = "CY"
MONTH = "month"
POINT_IN_TIME = "point_in_time"

# Indian fiscal quarters: the year opens in April.
FY_QUARTER_BY_MONTH = {
    4: 1, 5: 1, 6: 1,
    7: 2, 8: 2, 9: 2,
    10: 3, 11: 3, 12: 3,
    1: 4, 2: 4, 3: 4,
}

MONTH_NAMES = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

POINT_MARKERS = ("as at", "as of", "as on", "outstanding at", "closing")


def expand_year(token: str, century_hint: int | None = None) -> int | None:
    """Turn a written year into a four-digit one. '24' becomes 2024."""
    token = token.strip()
    if not token.isdigit():
        return None
    if len(token) == 4:
        return int(token)
    if len(token) == 2:
        base = (century_hint // 100) * 100 if century_hint else 2000
        return base + int(token)
    return None


def fiscal_year_of(year: int, month: int) -> int:
    """Which Indian fiscal year a calendar month falls in, by ending year."""
    return year + 1 if month >= 4 else year


@dataclass(frozen=True)
class Period:
    """A period reduced to something comparable.

    ``fiscal_year`` is the calendar year the fiscal year ends in, which is the
    token that makes different publishers' notations agree.
    """

    period_type: str | None = None
    fiscal_year: int | None = None
    sub_period: str | None = None
    raw: str | None = None

    @property
    def is_resolved(self) -> bool:
        return self.period_type is not None and self.fiscal_year is not None

    def signature(self) -> tuple:
        return (self.period_type, self.fiscal_year, self.sub_period)

    def _span(self) -> tuple[int, int] | None:
        """The period as a range of fiscal months, 1 = April, 12 = March."""
        if self.fiscal_year is None:
            return None

        if self.period_type == FY:
            return (1, 12)
        if self.period_type == QUARTER and self.sub_period:
            quarter = int(self.sub_period[1:])
            return (quarter * 3 - 2, quarter * 3)
        if self.period_type == HALF and self.sub_period:
            half = int(self.sub_period[1:])
            return (half * 6 - 5, half * 6)
        if self.period_type in (MONTH, POINT_IN_TIME) and self.sub_period:
            month = int(self.sub_period[1:])
            offset = month - 3 if month >= 4 else month + 9
            return (offset, offset)
        return None

    def contains(self, other: Period) -> bool:
        """True when ``other`` falls entirely inside this period.

        Used to tell a part-of-whole difference apart from a contradiction.
        """
        if not (self.is_resolved and other.is_resolved):
            return False
        if self.period_type == CY or other.period_type == CY:
            # Calendar and fiscal years overlap without nesting cleanly.
            return self.period_type == other.period_type and self == other
        if self.fiscal_year != other.fiscal_year:
            return False

        mine, theirs = self._span(), other._span()
        if mine is None or theirs is None:
            return False
        if mine == theirs:
            return False  # the same period, not a containment
        return mine[0] <= theirs[0] and theirs[1] <= mine[1]


def _match_fiscal_range(text: str) -> int | None:
    """FY2024-25, 2024-25, FY 2023/24 - the year it ends in."""
    match = re.search(
        r"(?:fy|f\.y\.?|fiscal|financial\s+year)?\s*"
        r"(\d{4})\s*[-/–—]\s*(\d{2}|\d{4})\b",
        text,
    )
    if not match:
        return None

    start = int(match.group(1))
    end = expand_year(match.group(2), century_hint=start)
    if end is None:
        return None
    # A range like 2024-25 spans one year; anything else is not a fiscal year.
    return end if 0 < end - start <= 1 else None


def _match_fiscal_single(text: str) -> int | None:
    """FY24, FY2024, FY 24 - already named for the year it ends in."""
    match = re.search(r"(?:fy|f\.y\.?)\s*[-']?\s*(\d{4}|\d{2})\b", text)
    if not match:
        return None
    return expand_year(match.group(1))


def _match_sub_period(text: str) -> tuple[str, str] | None:
    """A quarter or half marker, e.g. Q4 or H1."""
    quarter = re.search(r"\bq\s*([1-4])\b", text)
    if quarter:
        return QUARTER, f"Q{quarter.group(1)}"

    half = re.search(r"\bh\s*([12])\b", text)
    if half:
        return HALF, f"H{half.group(1)}"

    if "first half" in text:
        return HALF, "H1"
    if "second half" in text:
        return HALF, "H2"
    return None


def _match_month(text: str) -> tuple[int, int] | None:
    """A month and year written together.

    Handles 'March 2024', 'Mar-24', 'March 31, 2024' and '31 March 2024'. The
    day-bearing forms are tried first, so the day is never mistaken for a
    two-digit year.
    """
    names = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
    ordinal = r"(?:st|nd|rd|th)?"

    patterns = (
        # March 31, 2024
        (rf"\b({names})\w*\.?\s+\d{{1,2}}{ordinal}\s*,?\s*(\d{{4}})\b", 1, 2),
        # 31 March 2024
        (rf"\b\d{{1,2}}{ordinal}\s+({names})\w*\.?\s*,?\s*(\d{{4}})\b", 1, 2),
        # March 2024, Mar-24
        (rf"\b({names})\w*\.?\s*[-,]?\s*(\d{{4}}|\d{{2}})\b", 1, 2),
    )

    for pattern, month_group, year_group in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        year = expand_year(match.group(year_group))
        if year is None:
            continue
        return MONTH_NAMES[match.group(month_group)], year

    return None


def _period_keyword(text: str) -> str | None:
    """The kind of period a phrase like 'quarter ended ...' describes."""
    if re.search(r"\bas\s+(?:at|of|on)\b|\boutstanding\s+at\b", text):
        return POINT_IN_TIME
    if not re.search(r"\b(?:ended|ending|end\s+of|closed)\b", text):
        return None
    if re.search(r"\bquarter\b", text):
        return QUARTER
    if re.search(r"\bhalf[-\s]?year\b|\bhalf\b", text):
        return HALF
    if re.search(r"\b(?:fiscal|financial|full)?\s*year\b", text):
        return FY
    if re.search(r"\bmonth\b", text):
        return MONTH
    return None


def parse_period(text: str | None) -> Period:
    """Read a written period into something comparable.

    Anything unrecognised comes back unresolved rather than guessed at; the
    original text is always kept.
    """
    if not text or not text.strip():
        return Period()

    raw = text.strip()
    lowered = raw.lower()

    sub = _match_sub_period(lowered)
    fiscal = _match_fiscal_range(lowered)
    if fiscal is None:
        fiscal = _match_fiscal_single(lowered)

    # An explicit fiscal token is the strongest signal and wins outright.
    if fiscal is not None:
        if sub:
            return Period(period_type=sub[0], fiscal_year=fiscal, sub_period=sub[1], raw=raw)
        return Period(period_type=FY, fiscal_year=fiscal, raw=raw)

    month_hit = _match_month(lowered)
    if month_hit:
        month, year = month_hit
        fiscal_year = fiscal_year_of(year, month)
        keyword = _period_keyword(lowered)

        # "year ended 31 March 2024" is FY24; the closing date names the year.
        if keyword == FY:
            return Period(period_type=FY, fiscal_year=fiscal_year, raw=raw)
        if keyword == QUARTER or (sub and sub[0] == QUARTER):
            quarter = sub[1] if sub else f"Q{FY_QUARTER_BY_MONTH[month]}"
            return Period(QUARTER, fiscal_year, quarter, raw)
        if keyword == HALF or (sub and sub[0] == HALF):
            half = sub[1] if sub else ("H1" if FY_QUARTER_BY_MONTH[month] <= 2 else "H2")
            return Period(HALF, fiscal_year, half, raw)

        return Period(
            period_type=POINT_IN_TIME if keyword == POINT_IN_TIME else MONTH,
            fiscal_year=fiscal_year,
            sub_period=f"M{month:02d}",
            raw=raw,
        )

    # A quarter or half named against a bare year, e.g. "Q4 2024".
    if sub:
        bare_year = re.search(r"\b(\d{4})\b", lowered)
        if bare_year:
            return Period(sub[0], int(bare_year.group(1)), sub[1], raw)

    # A bare four-digit year with no fiscal marker is read as a calendar year.
    calendar = re.search(r"(?:cy|calendar\s+year)\s*(\d{4})\b", lowered)
    if calendar:
        return Period(period_type=CY, fiscal_year=int(calendar.group(1)), raw=raw)

    bare = re.fullmatch(r"\D*(\d{4})\D*", lowered)
    if bare:
        year = int(bare.group(1))
        if 1900 <= year <= 2200:
            return Period(period_type=CY, fiscal_year=year, raw=raw)

    return Period(raw=raw)
