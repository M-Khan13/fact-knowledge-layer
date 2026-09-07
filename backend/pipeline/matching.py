"""Finding pairs of facts that are talking about the same thing.

Matching proposes candidates; it does not judge them. Whether a pair
corroborates, contradicts or reconciles is decided later, from the context
signature. The only question here is: are these two facts about the same
subject and the same attribute?

Pairs are always drawn across different source documents. Two facts from one
document restating each other are not a cross-document agreement, and treating
them as one would inflate every count downstream.

Subjects are blocked on their resolved key. Where a document stated an official
identifier that key is authoritative and must match exactly - two different DINs
are two different people, however similar the names. Only weaker name-based
keys are allowed to match on similarity, which is exactly where spelling
variation actually happens.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from itertools import combinations

from backend import store
from backend.pipeline.embeddings import CachedEmbedder, Embedder, cosine, default_embedder
from backend.pipeline.schema import Fact

# Thresholds are calibrated against the local fallback embedder, whose scale is
# lexical rather than semantic. A stronger embedder separates meanings better
# and should be re-tuned; every threshold is a parameter for that reason.

# A weak (name-derived) subject key may match another by similarity above this.
# Deliberately strict: wrongly merging two entities corrupts every pair drawn
# from them, while failing to merge only costs a missed candidate.
SUBJECT_SIMILARITY_THRESHOLD = 0.82

# Two differently-worded attributes describe the same measure above this. Set
# low enough to admit pairs that differ only in a qualifier - "real GDP growth"
# against "nominal GDP growth" is one attribute measured two ways, and the
# basis field, not the threshold, is what tells them apart afterwards.
ATTRIBUTE_SIMILARITY_THRESHOLD = 0.55

# Candidates below this combined score are not worth adjudicating.
MIN_PAIR_SCORE = 0.50

STRONG_KEY_PREFIXES = ("DIN:", "CIN:")


@dataclass(frozen=True)
class CandidatePair:
    """Two facts that appear to be about the same thing, and why."""

    fact_a: Fact
    fact_b: Fact
    score: float
    subject_similarity: float
    attribute_similarity: float
    subject_match: str
    attribute_match: str

    @property
    def key(self) -> tuple[str, str]:
        """Order-independent identity, so a pair is never counted twice."""
        return tuple(sorted((self.fact_a.fact_id, self.fact_b.fact_id)))

    def as_dict(self) -> dict:
        return {
            "fact_a": self.fact_a.fact_id,
            "fact_b": self.fact_b.fact_id,
            "score": round(self.score, 4),
            "subject_similarity": round(self.subject_similarity, 4),
            "attribute_similarity": round(self.attribute_similarity, 4),
            "subject_match": self.subject_match,
            "attribute_match": self.attribute_match,
        }


def is_strong_key(subject_key: str | None) -> bool:
    """Whether a key came from an official identifier rather than a name."""
    return bool(subject_key) and subject_key.startswith(STRONG_KEY_PREFIXES)


def subject_text(fact: Fact) -> str:
    return fact.subject or (fact.subject_key or "")


def attribute_text(fact: Fact) -> str:
    """What is being measured, read as words rather than as an identifier."""
    return (fact.attribute or "").replace("_", " ").strip()


def _vectors(texts: list[str], embedder: Embedder) -> dict[str, list[float]]:
    unique = list(dict.fromkeys(t for t in texts if t))
    if not unique:
        return {}
    return dict(zip(unique, embedder.embed(unique)))


def _subject_blocks(
    facts: list[Fact], vectors: dict[str, list[float]]
) -> list[list[Fact]]:
    """Group facts into blocks that could be about one subject.

    This only bounds how many pairs are considered; how alike two subjects
    actually are is scored per pair afterwards. A strong key forms a block on
    its own, since an official identifier is authoritative. Weak name keys are
    merged where they look alike, so one entity written two ways is not split
    in two and never gets the chance to be compared.
    """
    by_key: dict[str, list[Fact]] = {}
    for fact in facts:
        by_key.setdefault(fact.subject_key or "", []).append(fact)

    blocks: list[list[Fact]] = []
    weak_keys: list[str] = []

    for key, group in by_key.items():
        if is_strong_key(key):
            blocks.append(group)
        else:
            weak_keys.append(key)

    # Largest group first, so a dominant spelling attracts the variants rather
    # than a rare one anchoring the group.
    merged: list[list[str]] = []
    for key in sorted(weak_keys, key=lambda k: -len(by_key[k])):
        for group_keys in merged:
            if _key_similarity(group_keys[0], key, by_key, vectors) >= (
                SUBJECT_SIMILARITY_THRESHOLD
            ):
                group_keys.append(key)
                break
        else:
            merged.append([key])

    blocks.extend(
        [fact for key in group_keys for fact in by_key[key]] for group_keys in merged
    )
    return blocks


def _key_similarity(
    first: str, second: str, by_key: dict[str, list[Fact]], vectors: dict[str, list[float]]
) -> float:
    """How alike two weak subject keys are, judged on their written subjects."""
    if first == second:
        return 1.0
    left = subject_text(by_key[first][0])
    right = subject_text(by_key[second][0])
    if left in vectors and right in vectors:
        return cosine(vectors[left], vectors[right])
    return 0.0


def _score_subject(
    first: Fact, second: Fact, vectors: dict[str, list[float]]
) -> tuple[float, str]:
    """How confidently two facts are about the same subject.

    Judged per pair rather than per block: a block merged to catch a spelling
    variant must not drag down the pairs inside it that agree exactly.
    """
    left_key, right_key = first.subject_key or "", second.subject_key or ""

    if left_key and left_key == right_key:
        return 1.0, "strong_key" if is_strong_key(left_key) else "same_key"

    left, right = subject_text(first), subject_text(second)
    if left in vectors and right in vectors:
        return cosine(vectors[left], vectors[right]), "similar_name"
    return 0.0, "similar_name"


def _score_attribute(
    first: Fact, second: Fact, vectors: dict[str, list[float]]
) -> tuple[float, str]:
    if first.attribute == second.attribute:
        return 1.0, "exact"

    left, right = attribute_text(first), attribute_text(second)
    if left and right and left == right:
        return 1.0, "exact"

    if left in vectors and right in vectors:
        return cosine(vectors[left], vectors[right]), "similar"
    return 0.0, "similar"


def find_candidates(
    conn: sqlite3.Connection,
    collection_id: str,
    *,
    embedder: Embedder | None = None,
    new_fact_ids: set[str] | None = None,
    min_score: float = MIN_PAIR_SCORE,
    attribute_threshold: float = ATTRIBUTE_SIMILARITY_THRESHOLD,
    limit: int | None = None,
) -> list[CandidatePair]:
    """Rank the fact pairs in a collection that look like the same thing.

    ``new_fact_ids`` restricts results to pairs involving at least one of those
    facts, which is what lets a newly added document be matched against the
    collection without re-examining pairs that were already considered.
    """
    facts = store.list_facts(conn, collection_id)
    if len(facts) < 2:
        return []

    if embedder is None:
        embedder = CachedEmbedder(default_embedder(), conn)

    vectors = _vectors(
        [subject_text(f) for f in facts] + [attribute_text(f) for f in facts], embedder
    )

    candidates: list[CandidatePair] = []
    seen: set[tuple[str, str]] = set()

    for block in _subject_blocks(facts, vectors):
        for first, second in combinations(block, 2):
            # Cross-document only: a document agreeing with itself is not news.
            if first.source_doc == second.source_doc:
                continue
            if new_fact_ids is not None and not (
                first.fact_id in new_fact_ids or second.fact_id in new_fact_ids
            ):
                continue

            pair_key = tuple(sorted((first.fact_id, second.fact_id)))
            if pair_key in seen:
                continue

            attribute_similarity, attribute_match = _score_attribute(
                first, second, vectors
            )
            if attribute_similarity < attribute_threshold:
                continue

            subject_similarity, subject_match = _score_subject(first, second, vectors)
            if subject_similarity < SUBJECT_SIMILARITY_THRESHOLD:
                continue

            score = subject_similarity * attribute_similarity
            if score < min_score:
                continue

            seen.add(pair_key)
            candidates.append(
                CandidatePair(
                    fact_a=first,
                    fact_b=second,
                    score=score,
                    subject_similarity=subject_similarity,
                    attribute_similarity=attribute_similarity,
                    subject_match=subject_match,
                    attribute_match=attribute_match,
                )
            )

    candidates.sort(key=lambda pair: (-pair.score, pair.key))
    return candidates[:limit] if limit else candidates
