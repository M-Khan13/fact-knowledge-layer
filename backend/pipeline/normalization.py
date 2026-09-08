"""Turning an extracted fact into a comparable one.

This is where the context signature is built. A fact is a value plus the
conditions under which that value holds, and comparison gates on those
conditions before any judgement is made about the numbers.

Nothing here decides whether two facts agree - it decides whether they are
even talking about the same thing. Values that cannot be resolved to a unit
keep a low confidence and stay uncomparable, which is what stops a
unit-blind comparison from ever producing a false contradiction.
"""

from __future__ import annotations

import re
from backend.pipeline import entities
from backend.pipeline.schema import ContextSignature, Fact
from backend.pipeline.temporal import parse_period
from backend.pipeline.units import ResolvedUnit, parse_number, parse_unit

# Confidence ceiling for a fact whose unit could not be resolved. Such a fact is
# never auto-contradicted; it can only ever reach no-verdict.
UNRESOLVED_UNIT_CONFIDENCE = 0.4

CONSOLIDATED = "consolidated"
STANDALONE = "standalone"

VINTAGE_ADVANCE = "advance_estimate"
VINTAGE_PROVISIONAL = "provisional"
VINTAGE_REVISED = "revised"
VINTAGE_FINAL = "final"
VINTAGE_PROJECTION = "projection"

# How settled a figure is. A later stage supersedes an earlier one, which makes
# a difference across vintages a revision rather than a disagreement.
VINTAGE_ORDER = {
    VINTAGE_ADVANCE: 0,
    VINTAGE_PROVISIONAL: 1,
    VINTAGE_REVISED: 2,
    VINTAGE_FINAL: 3,
}

# Checked in order: "first advance estimate" must not be read as "estimate".
VINTAGE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"advance\s+estimate|first\s+advance|\bfae\b|\bae\b", VINTAGE_ADVANCE),
    (r"provisional|\bpe\b", VINTAGE_PROVISIONAL),
    (r"revised|\bre\b|\bsre\b", VINTAGE_REVISED),
    (r"projection|projected|forecast|expected|outlook|\bproj\b", VINTAGE_PROJECTION),
    (r"final|actual|audited", VINTAGE_FINAL),
)

# Relative tolerance for agreement, and the floor for percentage-style values.
RELATIVE_TOLERANCE = 0.005
PERCENTAGE_POINT_TOLERANCE = 0.1


def canonical_token(text: str | None) -> str | None:
    """A stable token for free-text context, without imposing a vocabulary.

    Bracketed asides and filler words are dropped so "GDP (at market prices)"
    and "GDP at market prices" agree, but the wording is otherwise the
    document's own. An unfamiliar basis becomes its own token rather than
    failing to fit a fixed list.
    """
    if not text or not text.strip():
        return None

    lowered = text.lower().strip()
    lowered = re.sub(r"[(),]", " ", lowered)
    words = [w for w in re.split(r"[^a-z0-9%]+", lowered) if w]

    filler = {"the", "a", "an", "of", "for", "in", "on", "at", "to", "as", "per", "and"}
    kept = [w for w in words if w not in filler]

    token = "_".join(kept or words)
    return token or None


def normalize_scope(text: str | None) -> str | None:
    """Consolidated or standalone where the document says so."""
    if not text or not text.strip():
        return None

    lowered = text.lower()
    # Checked first: "unconsolidated" contains "consolidat" but means the opposite.
    if re.search(r"standalone|stand-alone|unconsolidated|non-consolidated", lowered):
        return STANDALONE
    if "consolidat" in lowered:
        return CONSOLIDATED
    return canonical_token(text)


def normalize_vintage(text: str | None) -> str | None:
    """How settled a figure is: an estimate, a revision, a final or a projection."""
    if not text or not text.strip():
        return None

    lowered = text.lower()
    for pattern, vintage in VINTAGE_PATTERNS:
        if re.search(pattern, lowered):
            return vintage
    return canonical_token(text)


def normalize_basis(text: str | None) -> str | None:
    """Which measure a value uses. Free text, canonicalised but not constrained."""
    return canonical_token(text)


def vintage_rank(vintage: str | None) -> int | None:
    """Where a vintage sits in the revision sequence, if it is in one."""
    return VINTAGE_ORDER.get(vintage) if vintage else None


def newer_vintage(first: str | None, second: str | None) -> str | None:
    """Whichever of two vintages supersedes the other, or None if unordered."""
    left, right = vintage_rank(first), vintage_rank(second)
    if left is None or right is None or left == right:
        return None
    return first if left > right else second


def build_signature(
    period: str | None,
    scope: str | None,
    basis: str | None,
    vintage: str | None,
) -> ContextSignature:
    return ContextSignature(
        period=parse_period(period),
        scope=normalize_scope(scope),
        basis=normalize_basis(basis),
        vintage=normalize_vintage(vintage),
    )


def values_agree(
    first: float | None,
    second: float | None,
    unit: ResolvedUnit | None = None,
    *,
    decimals: int | None = None,
) -> bool:
    """Whether two same-context values agree.

    The band is the larger of a relative tolerance and one unit in the last
    reported decimal, so a figure quoted to fewer decimals is not made to
    disagree with a more precise one. Percentage-style values get a flat
    tolerance instead, because rounding there is measured in points.
    """
    if first is None or second is None:
        return False

    difference = abs(first - second)

    if unit is not None and unit.family in {"percent", "percentage_point"}:
        return difference <= PERCENTAGE_POINT_TOLERANCE + 1e-12

    relative = RELATIVE_TOLERANCE * max(abs(first), abs(second))
    last_decimal = 10.0**-decimals if decimals is not None else 0.0

    return difference <= max(relative, last_decimal) + 1e-12


def decimals_of(value_raw: str | None) -> int | None:
    """How many decimal places a value was reported to."""
    if not value_raw:
        return None
    match = re.search(r"\.(\d+)", value_raw)
    return len(match.group(1)) if match else 0


def normalize_fact(fact: Fact) -> Fact:
    """Populate a fact's value_num, unit and context signature, in place.

    Confidence is capped where the unit could not be resolved: such a fact is
    still worth keeping and showing, but it must never be compared as though
    its number meant something definite.
    """
    unit = parse_unit(fact.unit, fact.value_raw)
    number = parse_number(fact.value_raw)

    fact.unit_raw = fact.unit_raw or fact.unit
    fact.unit_canonical = unit.canonical if unit else None
    fact.unit_family = unit.family if unit else None
    fact.value_num = number * unit.scale if (number is not None and unit) else None

    fact.signature = build_signature(
        fact.context.period,
        fact.context.scope,
        fact.context.basis,
        fact.context.vintage,
    )

    fact.subject_key = entities.resolve_subject_key(
        fact.subject, fact.subject_key, fact.evidence_span
    )

    if unit is None or fact.value_num is None:
        fact.confidence = min(fact.confidence, UNRESOLVED_UNIT_CONFIDENCE)

    fact.normalized = True
    return fact


def resolve_self_references(facts: list[Fact]) -> list[Fact]:
    """Bind a document's oblique self-references to the entity it is about.

    "the Company" means whichever entity that document actually names. That is
    worked out per document from the facts themselves, so it holds for a
    document nobody has seen before.
    """
    by_document: dict[str | None, list[Fact]] = {}
    for fact in facts:
        by_document.setdefault(fact.source_doc, []).append(fact)

    for document_facts in by_document.values():
        named = [
            fact.subject_key
            for fact in document_facts
            if fact.subject_key and not entities.is_self_reference(fact.subject)
        ]
        for fact in document_facts:
            kind = entities.self_reference_kind(fact.subject or "")
            if kind is None:
                continue
            # Resolved per kind: a document naming its directors is full of
            # person identifiers, and "the Group" is not one of them.
            main = entities.dominant_entity(named, kind=kind)
            if main:
                fact.subject_key = main

    return facts


BOARD_STATUS_ATTRIBUTE = "board_status"

# What a board fact says about whether someone is still on the board. Checked
# in order, and only for a subject identified by a director's number, so an
# ordinary "designation" elsewhere in a document is left alone.
BOARD_STATUS_RULES: tuple[tuple[str, str], ...] = (
    (r"resign|cessation|ceased|demitted|stepped[_\s-]?down", "resigned"),
    (r"\bdesignation\b|\bappointment\b|\brole\b|\bposition\b|\bboard[_\s-]?status\b",
     "active"),
)


def normalize_board_status(facts: list[Fact]) -> list[Fact]:
    """Give board facts one attribute so they can be compared at all.

    One filing records a director's designation, another records the date they
    resigned. Both answer the same question - is this person on the board - but
    under different attribute names, so they never meet. Mapping them onto a
    shared attribute with a category value is what lets the two documents
    disagree out loud.

    The written value and its evidence are untouched; the original attribute is
    kept alongside. Only subjects identified by a director's number qualify, so
    a "designation" that is not about a director is unaffected.
    """
    for fact in facts:
        if not (fact.subject_key or "").startswith("DIN:"):
            continue
        attribute = re.sub(r"[_\-]+", " ", (fact.attribute or "").lower())
        for pattern, status in BOARD_STATUS_RULES:
            if re.search(pattern, attribute):
                fact.attribute_raw = fact.attribute_raw or fact.attribute
                fact.attribute = BOARD_STATUS_ATTRIBUTE
                fact.category = status
                break
    return facts


def bind_strong_keys(facts: list[Fact]) -> list[Fact]:
    """Upgrade name-based subjects to an identifier the document states for them.

    A filing records a director's identification number as a fact of its own,
    and separately records facts about that director by name. Those are the same
    person, and only the identifier says so reliably. Where one fact states an
    identifier for a subject, every fact about that subject is re-keyed to it,
    so a name in one document and an identifier in another meet.

    The mapping is built from the subject's *name*, not from its current key:
    the fact that states the identifier has usually already been re-keyed by it,
    which would otherwise hide the very link being looked for.

    A name that two different identifiers claim is left alone. That is the
    table row-bleed case, where one row's identifier lands beside the next row's
    name, and picking a winner would invent an identity.
    """
    claims: dict[str, set[str]] = {}
    for fact in facts:
        stated = entities.identifier_stated_by(
            fact.attribute, fact.value_raw, fact.evidence_span
        )
        if not stated:
            continue
        name = entities.normalize_name(fact.subject or "")
        if name:
            claims.setdefault(f"name:{name}", set()).add(stated)

    upgrades = {
        key: identifiers.pop()
        for key, identifiers in claims.items()
        if len(identifiers) == 1
    }
    if not upgrades:
        return facts

    for fact in facts:
        replacement = upgrades.get(fact.subject_key or "")
        if replacement:
            fact.subject_key = replacement

    return facts


def normalize_facts(facts: list[Fact]) -> list[Fact]:
    """Normalize a batch, resolve self-references, then bind stated identifiers."""
    for fact in facts:
        normalize_fact(fact)
    resolve_self_references(facts)
    bind_strong_keys(facts)
    # Runs last: it depends on subjects already carrying their identifier.
    return normalize_board_status(facts)


def is_comparable(first: Fact, second: Fact) -> bool:
    """Whether two facts may be compared at all.

    Both must carry a resolved unit in the same family and base. This is the
    unit-blind comparison ban, enforced in one place.
    """
    if first.value_num is None or second.value_num is None:
        return False
    if not (first.unit_canonical and second.unit_canonical):
        return False
    return (
        first.unit_family == second.unit_family
        and first.unit_canonical == second.unit_canonical
    )
