"""Matching tests.

Facts are written straight into the store here rather than extracted, because
what is under test is which pairs get proposed - not where a value came from.
The subjects and attributes are notations a report might use; no value is
treated as true, and no verdict is reached.
"""

import pytest

from backend import store
from backend.pipeline.embeddings import (
    CachedEmbedder,
    HashingEmbedder,
    cosine,
    default_embedder,
)
from backend.pipeline.matching import (
    ATTRIBUTE_SIMILARITY_THRESHOLD,
    CandidatePair,
    find_candidates,
    is_strong_key,
)
from backend.pipeline.normalization import normalize_fact
from backend.pipeline.schema import Context, Fact, make_fact_id


def put(
    db, subject_key, attribute, doc, *, subject=None, value="1", unit="₹ Cr",
    collection="c", **ctx,
):
    """Store one fact and return it."""
    fact = Fact(
        fact_id=make_fact_id(
            collection, doc, attribute, value, f"{subject_key}{attribute}{doc}"
        ),
        collection_id=collection,
        subject=subject or subject_key,
        subject_key=subject_key,
        attribute=attribute,
        value_raw=value,
        value_num=None,
        unit=unit,
        context=Context(**ctx),
        source_doc=doc,
        page=1,
        evidence_span=f"evidence for {attribute} in {doc}",
        confidence=0.9,
    )
    normalize_fact(fact)
    fact.subject_key = subject_key  # keep the key the test asked for
    store.save_facts(db, [fact])
    return fact


@pytest.fixture
def collection(db):
    store.upsert_collection(db, "c")
    return db


@pytest.fixture
def embedder():
    return HashingEmbedder()


# --- Blocking --------------------------------------------------------------


def test_same_subject_and_attribute_across_documents_is_a_candidate(collection, embedder):
    a = put(collection, "name:acme", "revenue_from_operations", "doc_a")
    b = put(collection, "name:acme", "revenue_from_operations", "doc_b")

    pairs = find_candidates(collection, "c", embedder=embedder)

    assert len(pairs) == 1
    assert pairs[0].key == tuple(sorted((a.fact_id, b.fact_id)))
    assert pairs[0].attribute_match == "exact"
    assert pairs[0].score == pytest.approx(1.0)


def test_facts_from_one_document_are_never_paired(collection, embedder):
    """A document agreeing with itself is not a cross-document agreement."""
    put(collection, "name:acme", "revenue", "doc_a", value="1")
    put(collection, "name:acme", "revenue", "doc_a", value="2")

    assert find_candidates(collection, "c", embedder=embedder) == []


def test_different_subjects_are_not_paired(collection, embedder):
    put(collection, "name:acme logistics", "revenue", "doc_a")
    put(collection, "name:zenith freight", "revenue", "doc_b")

    assert find_candidates(collection, "c", embedder=embedder) == []


def test_unrelated_attributes_are_not_paired(collection, embedder):
    put(collection, "name:acme", "revenue_from_operations", "doc_a")
    put(collection, "name:acme", "headcount", "doc_b")

    assert find_candidates(collection, "c", embedder=embedder) == []


def test_differently_worded_attributes_still_match(collection, embedder):
    """Two documents rarely name a measure the same way."""
    put(collection, "name:acme", "revenue_from_operations", "doc_a")
    put(collection, "name:acme", "operating_revenue", "doc_b")

    pairs = find_candidates(collection, "c", embedder=embedder)

    assert len(pairs) == 1
    assert pairs[0].attribute_match == "similar"
    assert pairs[0].attribute_similarity >= ATTRIBUTE_SIMILARITY_THRESHOLD


def test_a_qualifier_difference_still_makes_a_candidate(collection, embedder):
    """Real against nominal is one measure two ways; basis separates them later."""
    put(collection, "name:india", "real_gdp_growth", "doc_a", basis="real")
    put(collection, "name:india", "nominal_gdp_growth", "doc_b", basis="nominal")

    pairs = find_candidates(collection, "c", embedder=embedder)

    assert len(pairs) == 1
    assert pairs[0].fact_a.signature.basis != pairs[0].fact_b.signature.basis


# --- Strong keys -----------------------------------------------------------


def test_strong_keys_must_match_exactly(collection, embedder):
    """Two DINs are two people, however alike the names look."""
    put(collection, "DIN:01344131", "board_status", "doc_a", subject="Sahil Barua")
    put(collection, "DIN:09876543", "board_status", "doc_b", subject="Sahil Barva")

    assert find_candidates(collection, "c", embedder=embedder) == []


def test_the_same_strong_key_pairs_across_documents(collection, embedder):
    put(collection, "DIN:01344131", "board_status", "doc_a", subject="Sahil Barua")
    put(collection, "DIN:01344131", "board_status", "doc_b", subject="S. Barua")

    pairs = find_candidates(collection, "c", embedder=embedder)

    assert len(pairs) == 1
    assert pairs[0].subject_match == "strong_key"


def test_is_strong_key():
    assert is_strong_key("DIN:01344131")
    assert is_strong_key("CIN:L63090DL2011PLC221234")
    assert not is_strong_key("name:acme")
    assert not is_strong_key(None)


# --- Ranking and incremental behaviour -------------------------------------


def test_candidates_are_ranked_with_exact_matches_first(collection, embedder):
    put(collection, "name:acme", "revenue_from_operations", "doc_a")
    put(collection, "name:acme", "revenue_from_operations", "doc_b")
    put(collection, "name:acme", "operating_revenue", "doc_c")

    pairs = find_candidates(collection, "c", embedder=embedder)

    assert len(pairs) >= 2
    assert pairs[0].score >= pairs[-1].score
    assert pairs[0].attribute_match == "exact"


def test_matching_only_new_facts_skips_already_considered_pairs(collection, embedder):
    """Adding a document must not re-examine pairs the collection already had."""
    put(collection, "name:acme", "revenue", "doc_a")
    put(collection, "name:acme", "revenue", "doc_b")
    before = find_candidates(collection, "c", embedder=embedder)

    new = put(collection, "name:acme", "revenue", "doc_c")
    incremental = find_candidates(
        collection, "c", embedder=embedder, new_fact_ids={new.fact_id}
    )

    assert len(before) == 1
    assert len(incremental) == 2, "only the pairs involving the new fact"
    assert all(new.fact_id in pair.key for pair in incremental)


def test_limit_truncates_the_ranking(collection, embedder):
    for doc in ("doc_a", "doc_b", "doc_c", "doc_d"):
        put(collection, "name:acme", "revenue", doc)

    assert len(find_candidates(collection, "c", embedder=embedder, limit=2)) == 2


def test_a_pair_is_never_returned_twice(collection, embedder):
    put(collection, "name:acme", "revenue", "doc_a")
    put(collection, "name:acme", "revenue", "doc_b")

    pairs = find_candidates(collection, "c", embedder=embedder)
    keys = [pair.key for pair in pairs]

    assert len(keys) == len(set(keys))


def test_an_empty_or_single_fact_collection_yields_nothing(collection, embedder):
    assert find_candidates(collection, "c", embedder=embedder) == []
    put(collection, "name:acme", "revenue", "doc_a")
    assert find_candidates(collection, "c", embedder=embedder) == []


def test_collections_do_not_leak_into_each_other(collection, embedder):
    """A matching pair split across two collections is not a pair."""
    store.upsert_collection(collection, "other")
    put(collection, "name:acme", "revenue", "doc_a", collection="c")
    put(collection, "name:acme", "revenue", "doc_b", collection="other")

    assert find_candidates(collection, "c", embedder=embedder) == []
    assert find_candidates(collection, "other", embedder=embedder) == []


def test_pairs_carry_incomparable_units_through_for_later_judgement(collection, embedder):
    """Matching proposes; it does not decide. A unit clash is Phase 5's problem."""
    put(collection, "name:acme", "revenue", "doc_a", unit="₹ Cr")
    put(collection, "name:acme", "revenue", "doc_b", unit="%")

    assert len(find_candidates(collection, "c", embedder=embedder)) == 1


# --- Embedding behaviour ---------------------------------------------------


def test_cosine_bounds():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [-1.0, 0.0]) == 0.0  # clamped, not negative
    assert cosine([], [1.0]) == 0.0
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_hashing_embedder_is_deterministic_and_normalised():
    first = HashingEmbedder().embed(["revenue from operations"])[0]
    second = HashingEmbedder().embed(["revenue from operations"])[0]

    assert first == second
    assert sum(v * v for v in first) == pytest.approx(1.0)


def test_hashing_embedder_separates_unrelated_wording():
    vectors = HashingEmbedder().embed(["revenue from operations", "headcount"])

    assert cosine(*vectors) < 0.2


def test_embeddings_are_cached_and_reused(db):
    class CountingEmbedder:
        name = "counting"

        def __init__(self):
            self.calls = 0

        def embed(self, texts):
            self.calls += 1
            return [[1.0, 0.0] for _ in texts]

    inner = CountingEmbedder()
    cached = CachedEmbedder(inner, db)

    cached.embed(["one", "two"])
    cached.embed(["one", "two"])

    assert inner.calls == 1, "the second call should be served from the cache"


def test_cache_deduplicates_within_one_call(db):
    class CountingEmbedder:
        name = "counting"

        def __init__(self):
            self.seen = []

        def embed(self, texts):
            self.seen.extend(texts)
            return [[1.0, 0.0] for _ in texts]

    inner = CountingEmbedder()
    result = CachedEmbedder(inner, db).embed(["same", "same", "other"])

    assert inner.seen == ["same", "other"]
    assert len(result) == 3


def test_default_embedder_falls_back_without_a_key(monkeypatch):
    monkeypatch.setattr("backend.config.GEMINI_API_KEY", "")

    assert isinstance(default_embedder(), HashingEmbedder)


# --- Weak-name merging -----------------------------------------------------


def test_name_variants_of_one_entity_are_merged(collection, embedder):
    """A weak name key may absorb a near-identical spelling."""
    put(
        collection, "name:ministry of finance", "budget_deficit", "doc_a",
        subject="Ministry of Finance",
    )
    put(
        collection, "name:ministry of finances", "budget_deficit", "doc_b",
        subject="Ministry of Finances",
    )

    pairs = find_candidates(collection, "c", embedder=embedder)

    assert len(pairs) == 1
    assert pairs[0].subject_match == "similar_name"
    assert pairs[0].score < 1.0, "a fuzzy subject must rank below an exact one"


def test_merging_does_not_reach_across_genuinely_different_names(collection, embedder):
    put(
        collection, "name:ministry of finance", "budget_deficit", "doc_a",
        subject="Ministry of Finance",
    )
    put(
        collection, "name:department of transport", "budget_deficit", "doc_b",
        subject="Department of Transport",
    )

    assert find_candidates(collection, "c", embedder=embedder) == []


def test_an_exact_subject_outranks_a_fuzzy_one(collection, embedder):
    put(collection, "name:ministry of finance", "budget_deficit", "doc_a",
        subject="Ministry of Finance")
    put(collection, "name:ministry of finance", "budget_deficit", "doc_b",
        subject="Ministry of Finance")
    put(collection, "name:ministry of finances", "budget_deficit", "doc_c",
        subject="Ministry of Finances")

    pairs = find_candidates(collection, "c", embedder=embedder)

    assert pairs[0].subject_match == "same_key"
    assert any(pair.subject_match == "similar_name" for pair in pairs)
