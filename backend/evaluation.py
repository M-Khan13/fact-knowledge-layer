"""Scoring extraction against a hand-labelled fact set.

Labels are read from a file and never invented. The loader does not assume one
layout: it inspects the JSON, maps whatever field names it finds onto the
fields it needs, and reports exactly what it recognised so a misread shape is
visible rather than silent. A file it cannot understand is an error naming the
keys it did see, not a guess.

Matching reuses the pipeline's own normalization, so a label written as
"8,142 Cr" matches a fact extracted as "81,415.38 million" for the same reason
the reconciler would call them the same figure.

Misses are reported with a cause, because "the engine did not find this" and
"the engine found this and got the number wrong" are different failures and
need different fixes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from backend.pipeline.embeddings import HashingEmbedder, cosine
from backend.pipeline.entities import normalize_name
from backend.pipeline.normalization import canonical_token, decimals_of, values_agree
from backend.pipeline.schema import Fact
from backend.pipeline.temporal import parse_period
from backend.pipeline.units import parse_number, parse_unit

# Field names a label file might reasonably use, in preference order. The first
# one present wins. Unknown keys are kept on the label and reported, never
# silently dropped.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "document": (
        "document", "source_doc", "doc", "source", "file", "filename",
        "pdf", "doc_name", "document_name",
    ),
    "subject": ("subject", "entity", "about", "subject_name"),
    "attribute": ("attribute", "attr", "field", "measure", "metric", "name"),
    "value": ("value_raw", "value", "raw_value", "expected_value", "expected"),
    "unit": ("unit", "units"),
    "period": ("period", "fiscal_period", "time_period", "timeframe", "date"),
    "scope": ("scope",),
    "basis": ("basis",),
    "vintage": ("vintage",),
    "page": ("page", "page_number", "page_no", "pageno"),
    "label_id": ("label_id", "id", "ref"),
}

# Keys a top-level object might wrap the list of labels in.
LIST_KEYS = ("labels", "facts", "items", "data", "records", "examples", "rows")

ATTRIBUTE_SIMILARITY_THRESHOLD = 0.55

MISS_NOT_EXTRACTED = "not_extracted"
MISS_VALUE_MISMATCH = "value_mismatch"


class LabelFormatError(ValueError):
    """The label file could not be read as a list of labelled facts."""


@dataclass
class Label:
    """One hand-labelled fact, as read from the file."""

    document: str | None
    subject: str | None
    attribute: str | None
    value: str | None
    unit: str | None = None
    period: str | None = None
    scope: str | None = None
    basis: str | None = None
    vintage: str | None = None
    pages: list[int] = field(default_factory=list)
    label_id: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def page(self) -> int | None:
        """The first cited page, where one is given."""
        return self.pages[0] if self.pages else None

    @property
    def describe(self) -> str:
        parts = [self.attribute or "?", str(self.value or "?")]
        if self.unit:
            parts.append(self.unit)
        if self.period:
            parts.append(f"({self.period})")
        return " ".join(parts)


@dataclass
class LoadedLabels:
    """Labels plus what the loader made of the file, so a misread is visible."""

    labels: list[Label]
    path: Path
    detected_shape: str
    recognised_fields: dict[str, str]
    unmapped_keys: list[str]

    def __len__(self) -> int:
        return len(self.labels)

    def describe(self) -> str:
        lines = [
            f"file            : {self.path}",
            f"labels found    : {len(self.labels)}",
            f"shape           : {self.detected_shape}",
            "field mapping   :",
        ]
        for wanted, found in sorted(self.recognised_fields.items()):
            lines.append(f"    {wanted:<10} <- {found}")
        missing = [name for name in FIELD_ALIASES if name not in self.recognised_fields]
        if missing:
            lines.append(f"    not present: {', '.join(sorted(missing))}")
        if self.unmapped_keys:
            lines.append(f"unused keys     : {', '.join(sorted(self.unmapped_keys))}")
        documents = sorted({label.document for label in self.labels if label.document})
        lines.append(f"documents       : {', '.join(documents) or 'none named'}")
        return "\n".join(lines)


def _pick(entry: dict, aliases: tuple[str, ...]) -> tuple[str, object] | None:
    """The first aliased key present in this entry, matched case-insensitively."""
    lowered = {key.lower(): key for key in entry}
    for alias in aliases:
        actual = lowered.get(alias.lower())
        if actual is not None and entry[actual] not in (None, ""):
            return actual, entry[actual]
    return None


def _as_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    text = str(value).strip()
    return text or None


def _read_pages(value: object) -> list[int]:
    """Read a page citation, which may name more than one page.

    A fact often appears on several pages of the same document, so a label may
    cite `22`, `[6, 9, 17]` or `"6, 9, 17"`. Any of them is enough for the
    grounded page to be considered agreed.
    """
    if value is None:
        return []

    candidates: list[object]
    if isinstance(value, (list, tuple)):
        candidates = list(value)
    elif isinstance(value, str):
        candidates = [part for part in value.replace(";", ",").split(",")]
    else:
        candidates = [value]

    pages = []
    for candidate in candidates:
        try:
            pages.append(int(str(candidate).strip()))
        except (TypeError, ValueError):
            continue
    return pages


def _find_entries(payload: object) -> tuple[list[dict], str, str | None]:
    """Locate the list of label objects, whatever the file wraps them in.

    Returns the entries, a description of the shape, and the key documents were
    grouped under if the file was organised that way.
    """
    if isinstance(payload, list):
        if all(isinstance(item, dict) for item in payload):
            return payload, "a list of label objects", None
        raise LabelFormatError("the top-level list does not contain objects")

    if not isinstance(payload, dict):
        raise LabelFormatError(
            f"expected a list or object at the top level, got {type(payload).__name__}"
        )

    for key in LIST_KEYS:
        for actual in payload:
            if actual.lower() == key and isinstance(payload[actual], list):
                entries = payload[actual]
                if all(isinstance(item, dict) for item in entries):
                    return entries, f"an object with the labels under {actual!r}", None

    # A mapping of document name -> list of labels for that document.
    grouped = {
        key: value
        for key, value in payload.items()
        if isinstance(value, list) and all(isinstance(item, dict) for item in value)
    }
    if grouped and len(grouped) == len(
        [v for v in payload.values() if isinstance(v, list)]
    ):
        entries = []
        for document, items in grouped.items():
            for item in items:
                entries.append({**item, "__group__": document})
        return entries, "an object keyed by document name", "__group__"

    raise LabelFormatError(
        "could not find a list of labels. Top-level keys were: "
        + ", ".join(sorted(payload)[:20])
    )


def load_labels(path: str | Path) -> LoadedLabels:
    """Read labels from a JSON file, adapting to the shape it actually has."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(
            f"No label file at {path}. Point --labels at your labelled fact set; "
            "the harness will not run without one."
        )

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LabelFormatError(f"{path} is not valid JSON: {exc}") from exc

    entries, shape, group_key = _find_entries(payload)
    if not entries:
        raise LabelFormatError(f"{path} contains no labels")

    recognised: dict[str, str] = {}
    used_keys: set[str] = set()
    labels: list[Label] = []

    for entry in entries:
        values: dict[str, object] = {}
        for wanted, aliases in FIELD_ALIASES.items():
            found = _pick(entry, aliases)
            if found is None:
                continue
            actual_key, value = found
            values[wanted] = value
            used_keys.add(actual_key)
            recognised.setdefault(wanted, actual_key)

        if group_key and not values.get("document"):
            values["document"] = entry.get(group_key)

        pages = _read_pages(values.get("page"))

        labels.append(
            Label(
                document=_as_text(values.get("document")),
                subject=_as_text(values.get("subject")),
                attribute=_as_text(values.get("attribute")),
                value=_as_text(values.get("value")),
                unit=_as_text(values.get("unit")),
                period=_as_text(values.get("period")),
                scope=_as_text(values.get("scope")),
                basis=_as_text(values.get("basis")),
                vintage=_as_text(values.get("vintage")),
                pages=pages,
                label_id=_as_text(values.get("label_id")),
                raw=entry,
            )
        )

    if "attribute" not in recognised and "value" not in recognised:
        keys = sorted({key for entry in entries for key in entry})
        raise LabelFormatError(
            "no attribute or value field recognised in the labels. Keys present: "
            + ", ".join(keys[:20])
            + ". Rename them, or add the names to FIELD_ALIASES."
        )

    unmapped = sorted(
        {key for entry in entries for key in entry}
        - used_keys
        - ({group_key} if group_key else set())
    )

    return LoadedLabels(
        labels=labels,
        path=path,
        detected_shape=shape,
        recognised_fields=recognised,
        unmapped_keys=unmapped,
    )


# --- Matching --------------------------------------------------------------


def _document_matches(label: Label, fact: Fact) -> bool:
    """Whether a label and a fact refer to the same document.

    A label naming no document matches any, since a partial label set may
    simply not record it.
    """
    if not label.document:
        return True
    wanted = Path(label.document).stem.lower()
    return wanted == (fact.source_doc or "").lower()


def _subject_matches(label: Label, fact: Fact) -> bool:
    if not label.subject:
        return True
    left = normalize_name(label.subject)
    right = normalize_name(fact.subject or "")
    if not left or not right:
        return True
    return left == right or left in right or right in left


def _attribute_similarity(label: Label, fact: Fact, embedder) -> float:
    left = canonical_token(label.attribute or "")
    right = canonical_token(fact.attribute or "")
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    vectors = embedder.embed(
        [left.replace("_", " "), right.replace("_", " ")]
    )
    return cosine(*vectors)


def values_match(label: Label, fact: Fact) -> bool:
    """Whether a labelled value and an extracted one are the same figure.

    Compared numerically where both resolve to a unit, using the pipeline's own
    agreement band, and by written form otherwise.
    """
    label_unit = parse_unit(label.unit, label.value)
    label_number = parse_number(label.value)

    if label_number is not None and label_unit is not None and fact.has_resolved_unit:
        if label_unit.family == fact.unit_family and label_unit.canonical == fact.unit_canonical:
            decimals = [
                d for d in (decimals_of(label.value), decimals_of(fact.value_raw))
                if d is not None
            ]
            return values_agree(
                label_number * label_unit.scale,
                fact.value_num,
                label_unit,
                decimals=max(decimals) if decimals else None,
            )

    # No comparable units on one side: fall back to the bare numbers, then text.
    fact_number = parse_number(fact.value_raw)
    if label_number is not None and fact_number is not None:
        decimals = [
            d for d in (decimals_of(label.value), decimals_of(fact.value_raw))
            if d is not None
        ]
        return values_agree(
            label_number, fact_number, decimals=max(decimals) if decimals else None
        )

    left = (label.value or "").strip().casefold().replace(",", "").replace(" ", "")
    right = (fact.value_raw or "").strip().casefold().replace(",", "").replace(" ", "")
    return bool(left) and left == right


def period_matches(label: Label, fact: Fact) -> bool:
    """Whether the label's period agrees with the fact's, where one is given."""
    if not label.period:
        return True
    wanted = parse_period(label.period)
    if not wanted.is_resolved or fact.signature is None:
        return True
    got = fact.signature.period
    if not got.is_resolved:
        return False
    return wanted.signature() == got.signature()


@dataclass
class Match:
    """A label paired with the fact that satisfies it."""

    label: Label
    fact: Fact
    attribute_similarity: float
    page_agrees: bool

    @property
    def exact_attribute(self) -> bool:
        return self.attribute_similarity >= 1.0


@dataclass
class Miss:
    """A label the engine did not satisfy, and why."""

    label: Label
    cause: str
    nearest: Fact | None = None
    note: str = ""


@dataclass
class EvalResult:
    """What the run found, and the caveats on reading it."""

    matches: list[Match]
    misses: list[Miss]
    spurious: list[Fact]
    scoped_spurious: list[Fact]
    total_labels: int
    total_facts: int
    labelled_documents: list[str]

    @property
    def true_positives(self) -> int:
        return len(self.matches)

    @property
    def recall(self) -> float:
        return self.true_positives / self.total_labels if self.total_labels else 0.0

    @property
    def precision(self) -> float:
        """Against every extracted fact. Understates if labels are partial."""
        denominator = self.true_positives + len(self.spurious)
        return self.true_positives / denominator if denominator else 0.0

    @property
    def scoped_precision(self) -> float:
        """Restricted to attributes the label set actually covers."""
        denominator = self.true_positives + len(self.scoped_spurious)
        return self.true_positives / denominator if denominator else 0.0

    @property
    def f1(self) -> float:
        precision, recall = self.scoped_precision, self.recall
        return (
            2 * precision * recall / (precision + recall)
            if precision + recall
            else 0.0
        )

    @property
    def page_disagreements(self) -> list[Match]:
        return [m for m in self.matches if not m.page_agrees]


def evaluate(
    labels: list[Label],
    facts: list[Fact],
    *,
    embedder=None,
    attribute_threshold: float = ATTRIBUTE_SIMILARITY_THRESHOLD,
) -> EvalResult:
    """Score extracted facts against labels.

    Each label is satisfied by at most one fact and each fact satisfies at most
    one label, so nothing is counted twice. A label whose attribute was found
    but whose value disagrees is reported as a value mismatch rather than as a
    plain absence, because the two mean different things.
    """
    embedder = embedder or HashingEmbedder()

    scored: list[tuple[float, int, int, bool]] = []
    near_misses: dict[int, tuple[float, Fact]] = {}

    for label_index, label in enumerate(labels):
        for fact_index, fact in enumerate(facts):
            if not _document_matches(label, fact) or not _subject_matches(label, fact):
                continue
            similarity = _attribute_similarity(label, fact, embedder)
            if similarity < attribute_threshold:
                continue

            # Remember the closest attribute hit even if the value is wrong, so
            # the miss can say what the engine actually produced.
            best = near_misses.get(label_index)
            if best is None or similarity > best[0]:
                near_misses[label_index] = (similarity, fact)

            if values_match(label, fact) and period_matches(label, fact):
                scored.append((similarity, label_index, fact_index, True))

    # Greedy assignment, strongest attribute agreement first.
    scored.sort(key=lambda item: -item[0])
    taken_labels: set[int] = set()
    taken_facts: set[int] = set()
    matches: list[Match] = []

    for similarity, label_index, fact_index, _ in scored:
        if label_index in taken_labels or fact_index in taken_facts:
            continue
        taken_labels.add(label_index)
        taken_facts.add(fact_index)
        label, fact = labels[label_index], facts[fact_index]
        matches.append(
            Match(
                label=label,
                fact=fact,
                attribute_similarity=similarity,
                page_agrees=not label.pages or fact.page in label.pages,
            )
        )

    misses: list[Miss] = []
    for index, label in enumerate(labels):
        if index in taken_labels:
            continue
        near = near_misses.get(index)
        if near is None:
            misses.append(Miss(label=label, cause=MISS_NOT_EXTRACTED))
        else:
            _similarity, fact = near
            misses.append(
                Miss(
                    label=label,
                    cause=MISS_VALUE_MISMATCH,
                    nearest=fact,
                    note=f"engine produced {fact.value_raw} {fact.unit or ''}".strip(),
                )
            )

    spurious = [fact for index, fact in enumerate(facts) if index not in taken_facts]

    # Precision over the whole extraction punishes the engine for facts the
    # label set never intended to cover, so also report it scoped to the
    # attributes that were labelled at all.
    labelled_attributes = {
        canonical_token(label.attribute or "") for label in labels
    } - {None}
    scoped_spurious = [
        fact
        for fact in spurious
        if canonical_token(fact.attribute or "") in labelled_attributes
    ]

    return EvalResult(
        matches=matches,
        misses=misses,
        spurious=spurious,
        scoped_spurious=scoped_spurious,
        total_labels=len(labels),
        total_facts=len(facts),
        labelled_documents=sorted({label.document for label in labels if label.document}),
    )


def format_report(result: EvalResult, *, show: int = 30) -> str:
    """A readable summary, with the misses spelled out."""
    lines = [
        "",
        "=" * 72,
        "EXTRACTION EVALUATION",
        "=" * 72,
        f"  labels            {result.total_labels}",
        f"  facts extracted   {result.total_facts}",
        f"  matched           {result.true_positives}",
        "",
        f"  recall            {result.recall:.1%}   "
        f"({result.true_positives}/{result.total_labels} labels found)",
        f"  precision         {result.scoped_precision:.1%}   "
        f"(within labelled attributes; {len(result.scoped_spurious)} unmatched)",
        f"  F1                {result.f1:.1%}",
        "",
        f"  precision, all    {result.precision:.1%}   "
        f"(every extracted fact; {len(result.spurious)} unmatched)",
    ]

    if result.total_facts > result.total_labels:
        lines += [
            "",
            "  Note: a hand-labelled set is rarely exhaustive, so an unmatched",
            "  fact is not necessarily wrong. Read the scoped precision first,",
            "  and the list below to judge the rest.",
        ]

    if result.page_disagreements:
        lines += ["", f"  page disagreements  {len(result.page_disagreements)}"]
        for match in result.page_disagreements[:show]:
            lines.append(
                f"    {match.label.describe}: labelled p"
                f"{'/'.join(str(p) for p in match.label.pages)}, "
                f"grounded p{match.fact.page}"
            )

    not_found = [m for m in result.misses if m.cause == MISS_NOT_EXTRACTED]
    wrong_value = [m for m in result.misses if m.cause == MISS_VALUE_MISMATCH]

    lines += ["", "-" * 72, f"MISSED LABELS ({len(result.misses)})", "-" * 72]

    if wrong_value:
        lines.append(f"\n  Found the attribute, disagreed on the value ({len(wrong_value)}):")
        for miss in wrong_value[:show]:
            document = miss.label.document or "?"
            lines.append(f"    [{document}] {miss.label.describe}")
            lines.append(f"        {miss.note}")

    if not_found:
        lines.append(f"\n  Not extracted at all ({len(not_found)}):")
        for miss in not_found[:show]:
            document = miss.label.document or "?"
            lines.append(f"    [{document}] {miss.label.describe}")

    if not result.misses:
        lines.append("\n  None. Every label was matched.")

    lines += [
        "",
        "-" * 72,
        f"EXTRACTED BUT UNMATCHED, within labelled attributes "
        f"({len(result.scoped_spurious)})",
        "-" * 72,
    ]
    if result.scoped_spurious:
        for fact in result.scoped_spurious[:show]:
            lines.append(
                f"    [{fact.source_doc} p{fact.page}] {fact.attribute} = "
                f"{fact.value_raw} {fact.unit or ''}".rstrip()
            )
        if len(result.scoped_spurious) > show:
            lines.append(f"    ... and {len(result.scoped_spurious) - show} more")
    else:
        lines.append("    None.")

    lines.append("")
    return "\n".join(lines)


def result_to_dict(result: EvalResult) -> dict:
    """Machine-readable results, for tracking runs over time."""
    return {
        "totals": {
            "labels": result.total_labels,
            "facts_extracted": result.total_facts,
            "matched": result.true_positives,
        },
        "metrics": {
            "recall": round(result.recall, 4),
            "precision_scoped": round(result.scoped_precision, 4),
            "precision_all": round(result.precision, 4),
            "f1": round(result.f1, 4),
        },
        "misses": [
            {
                "cause": miss.cause,
                "document": miss.label.document,
                "attribute": miss.label.attribute,
                "value": miss.label.value,
                "unit": miss.label.unit,
                "period": miss.label.period,
                "pages": miss.label.pages,
                "label_id": miss.label.label_id,
                "note": miss.note,
                "nearest_fact_id": miss.nearest.fact_id if miss.nearest else None,
            }
            for miss in result.misses
        ],
        "unmatched_facts": [
            {
                "fact_id": fact.fact_id,
                "document": fact.source_doc,
                "page": fact.page,
                "attribute": fact.attribute,
                "value_raw": fact.value_raw,
                "unit": fact.unit,
                "within_labelled_attributes": fact in result.scoped_spurious,
            }
            for fact in result.spurious
        ],
        "page_disagreements": [
            {
                "label_pages": match.label.pages,
                "grounded_page": match.fact.page,
                "fact_id": match.fact.fact_id,
                "attribute": match.label.attribute,
            }
            for match in result.page_disagreements
        ],
    }
