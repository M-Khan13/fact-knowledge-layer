"""Resolving who or what a fact is about.

Names are weak keys. "S. Barua" and "Sahil Barua" look different and may be the
same person; two unrelated companies can share a word. Where a document states
an official identifier, that identifier decides identity instead:

* a Director Identification Number for a person,
* a Corporate Identity Number for a company.

Both are matched by their published shape, not against any list of known
entities, so an unseen document resolves on the same terms.

Where no strong key exists the normalized legal name is used. Documents also
refer to themselves obliquely - "the Company", "your Company" - and those are
bound to whichever named entity the document actually talks about, worked out
at runtime from the document's own facts.
"""

from __future__ import annotations

import re
from collections import Counter

# A Director Identification Number is eight digits, introduced by its name.
DIN_RE = re.compile(r"\bdin\b[:\s#-]*([0-9]{8})\b", re.IGNORECASE)

# A Corporate Identity Number: listing status, industry code, state, year,
# ownership class and registration number.
CIN_RE = re.compile(r"\b([LU][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6})\b", re.IGNORECASE)

# Dropped when comparing company names; they carry no distinguishing meaning.
LEGAL_SUFFIXES = {
    "limited", "ltd", "pvt", "private", "public", "plc", "inc", "incorporated",
    "corp", "corporation", "company", "co", "llp", "llc", "gmbh", "sa", "nv",
    "bv", "ag", "spa", "pte", "holdings", "group",
}

# A document referring to itself. These bind to the document's main entity.
SELF_REFERENCES = {
    "the company", "your company", "our company", "the issuer", "the group",
    "the bank", "the corporation", "the firm", "the organisation",
    "the organization", "we", "us", "the entity", "the parent",
}

HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "prof", "shri", "smt", "sri", "sh"}

# What kind of thing a key identifies. This is inherent in the identifier
# itself: a Director Identification Number names a person, a Corporate Identity
# Number names an organisation. A name says nothing either way.
PERSON = "person"
ORGANISATION = "organisation"

KEY_KINDS: dict[str, str] = {"DIN:": PERSON, "CIN:": ORGANISATION}


def entity_kind(subject_key: str | None) -> str | None:
    """What kind of entity a key identifies, where the key says so."""
    if not subject_key:
        return None
    for prefix, kind in KEY_KINDS.items():
        if subject_key.startswith(prefix):
            return kind
    return None


def find_din(*texts: str | None) -> str | None:
    """The first Director Identification Number stated in the given text."""
    for text in texts:
        if not text:
            continue
        match = DIN_RE.search(text)
        if match:
            return match.group(1)
    return None


def find_cin(*texts: str | None) -> str | None:
    """The first Corporate Identity Number stated in the given text."""
    for text in texts:
        if not text:
            continue
        match = CIN_RE.search(text)
        if match:
            return match.group(1).upper()
    return None


def normalize_name(name: str) -> str:
    """Reduce a name to its distinguishing words.

    Legal suffixes and honorifics are dropped, punctuation is flattened and
    case is folded, so "Delhivery Limited" and "delhivery ltd." agree without
    either spelling being written down anywhere.
    """
    if not name:
        return ""

    lowered = re.sub(r"[^\w\s]", " ", name.lower())
    words = [w for w in lowered.split() if w]

    while words and words[0] in HONORIFICS:
        words.pop(0)
    while words and words[-1] in LEGAL_SUFFIXES:
        words.pop()

    return " ".join(words)


def is_self_reference(subject: str) -> bool:
    """True for a document talking about itself rather than naming an entity."""
    cleaned = re.sub(r"[^\w\s]", " ", (subject or "").lower())
    cleaned = " ".join(cleaned.split())
    return cleaned in SELF_REFERENCES


def self_reference_kind(subject: str) -> str | None:
    """What kind of entity an oblique self-reference stands for.

    "the Company", "the Group", "the Bank" are all the reporting organisation.
    A filing never calls itself a person, so these must never bind to one.
    """
    return ORGANISATION if is_self_reference(subject) else None


def normalize_address(address: str) -> str:
    """Flatten an address so two spellings of one place agree."""
    if not address:
        return ""

    abbreviations = {
        r"\broad\b": "rd",
        r"\bstreet\b": "st",
        r"\bavenue\b": "ave",
        r"\bfloor\b": "fl",
        r"\bbuilding\b": "bldg",
        r"\bopposite\b": "opp",
        r"\bnear\b": "nr",
        r"\bnumber\b": "no",
        r"\bsector\b": "sec",
        r"\bphase\b": "ph",
    }

    text = re.sub(r"[^\w\s]", " ", address.lower())
    for pattern, short in abbreviations.items():
        text = re.sub(pattern, short, text)
    return " ".join(text.split())


def resolve_subject_key(
    subject: str,
    stated_key: str | None = None,
    *evidence: str | None,
) -> str | None:
    """The strongest identifier available for a subject.

    A stated official number always wins over a name, because it survives
    spelling differences that a name does not.
    """
    din = find_din(stated_key, subject, *evidence)
    if din:
        return f"DIN:{din}"

    cin = find_cin(stated_key, subject, *evidence)
    if cin:
        return f"CIN:{cin}"

    if stated_key and stated_key.strip():
        stated = stated_key.strip()
        # An identifier the document names in some other scheme.
        if ":" in stated:
            return stated
        normalized = normalize_name(stated)
        if normalized:
            return f"name:{normalized}"

    normalized = normalize_name(subject)
    return f"name:{normalized}" if normalized else None


# An attribute whose whole job is to carry an official identifier. Matched by
# what the attribute is called, not by any particular document's wording.
IDENTIFIER_ATTRIBUTE_RE = re.compile(
    r"\b(din|cin|identification[_\s-]?(?:no|number)|identity[_\s-]?(?:no|number)"
    r"|registration[_\s-]?(?:no|number))\b",
    re.IGNORECASE,
)

BARE_DIN_RE = re.compile(r"^\s*([0-9]{8})\s*$")
BARE_CIN_RE = re.compile(r"^\s*([LU][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6})\s*$", re.IGNORECASE)


def identifier_stated_by(
    attribute: str | None, value_raw: str | None, *evidence: str | None
) -> str | None:
    """The official identifier a fact states, if that is what the fact is for.

    A document often records a person's identifier as a fact in its own right -
    "DIN: 01173669" - separately from the facts about that person. Reading it
    here lets those facts be tied to the same entity as ones that name the
    identifier directly.
    """
    din = find_din(*evidence)
    if din:
        return f"DIN:{din}"
    cin = find_cin(*evidence)
    if cin:
        return f"CIN:{cin}"

    # The attribute says it holds an identifier, and the value is shaped like one.
    # Separators are flattened first: underscores are word characters, so
    # "director_identification_number" has no word boundary before "identification".
    flattened = re.sub(r"[_\-]+", " ", attribute or "")
    if flattened and IDENTIFIER_ATTRIBUTE_RE.search(flattened):
        value = value_raw or ""
        bare_cin = BARE_CIN_RE.match(value)
        if bare_cin:
            return f"CIN:{bare_cin.group(1).upper()}"
        bare_din = BARE_DIN_RE.match(value)
        if bare_din:
            return f"DIN:{bare_din.group(1)}"
    return None


def dominant_entity(subject_keys: list[str], kind: str | None = None) -> str | None:
    """The entity a document is mostly about, used to bind self-references.

    ``kind`` restricts the answer to entities that could be the thing being
    referred to. A filing that names its directors carries their identification
    numbers, and those are the strongest keys in the document - but "the Group"
    is not a director, so a key of the wrong kind is not a candidate at all.
    """
    candidates = [key for key in subject_keys if key]
    if kind is not None:
        candidates = [
            key for key in candidates if entity_kind(key) in (None, kind)
        ]
    if not candidates:
        return None

    # A stated identifier outranks a name, however often the name appears.
    strong = [key for key in candidates if entity_kind(key) is not None]
    pool = strong or candidates
    return Counter(pool).most_common(1)[0][0]
