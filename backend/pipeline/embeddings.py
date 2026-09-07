"""Embedding text so near-identical wording can be recognised as the same thing.

Two documents rarely name a measure the same way. "revenue_from_operations"
and "operating revenue" are one attribute written twice, and no fixed list can
anticipate that, so similarity is measured rather than looked up.

Two providers implement the same interface. Gemini is used when a key is
configured; otherwise a deterministic local embedder based on feature hashing
takes over, so matching still works - and stays reproducible - offline. Vectors
are cached by content, because the same handful of attribute strings recurs
across every document in a collection.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from typing import Protocol, Sequence

from backend import config

DEFAULT_DIMENSIONS = 512
MAX_ATTEMPTS = 4
BASE_BACKOFF_SECONDS = 1.0
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}

EMBEDDING_SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
    text_hash  TEXT NOT NULL,
    model      TEXT NOT NULL,
    vector     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (text_hash, model)
);
"""


class Embedder(Protocol):
    """Turns strings into unit vectors that can be compared by cosine."""

    name: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def cosine(first: Sequence[float], second: Sequence[float]) -> float:
    """Cosine similarity, clamped to [0, 1] since negatives mean 'unrelated'."""
    if not first or not second or len(first) != len(second):
        return 0.0

    dot = sum(a * b for a, b in zip(first, second))
    left = math.sqrt(sum(a * a for a in first))
    right = math.sqrt(sum(b * b for b in second))
    if left == 0.0 or right == 0.0:
        return 0.0

    return max(0.0, min(1.0, dot / (left * right)))


def _tokens(text: str) -> list[str]:
    """Split on anything that is not a letter or digit, including snake_case."""
    return [token for token in re.split(r"[^a-z0-9]+", text.lower()) if token]


def _features(text: str) -> list[str]:
    """Words plus character trigrams, so partial word overlap still counts."""
    words = _tokens(text)
    features = list(words)

    for word in words:
        padded = f" {word} "
        features.extend(padded[i : i + 3] for i in range(len(padded) - 2))

    # Adjacent word pairs keep some ordering information.
    features.extend(f"{a}_{b}" for a, b in zip(words, words[1:]))
    return features


class HashingEmbedder:
    """A deterministic local embedder using feature hashing.

    No network and no model download: features are hashed into a fixed number
    of buckets and the vector is L2-normalized. Similar wording lands in
    overlapping buckets, which is enough to tell "revenue from operations"
    apart from "headcount" while still relating it to "operating revenue".
    """

    name = "hashing-v1"

    def __init__(self, dimensions: int = DEFAULT_DIMENSIONS):
        self.dimensions = dimensions

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for feature in _features(text):
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dimensions
            # The last bit picks a sign, so unrelated features can cancel.
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]


class GeminiEmbedder:
    """Embeddings from the Gemini API, batched and retried."""

    def __init__(self, client=None, model: str | None = None, batch_size: int = 32):
        self._client = client
        self.model = model or config.GEMINI_EMBEDDING_MODEL
        self.batch_size = batch_size
        self.name = self.model

    @property
    def client(self):
        if self._client is None:
            from backend.pipeline.extraction import get_client

            self._client = get_client()
        return self._client

    def _embed_batch(self, batch: Sequence[str], sleep=time.sleep) -> list[list[float]]:
        from google.genai import errors

        last_error: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = self.client.models.embed_content(
                    model=self.model, contents=list(batch)
                )
                return [list(item.values) for item in response.embeddings]
            except errors.APIError as exc:
                status = getattr(exc, "code", None)
                if status not in RETRYABLE_STATUS or attempt == MAX_ATTEMPTS - 1:
                    raise
                last_error = exc
                sleep(BASE_BACKOFF_SECONDS * (2**attempt))

        raise last_error if last_error else RuntimeError("embedding failed")

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            vectors.extend(self._embed_batch(texts[start : start + self.batch_size]))
        return vectors


def default_embedder() -> Embedder:
    """Gemini where a key is configured, the local embedder otherwise."""
    if config.has_gemini_key():
        return GeminiEmbedder()
    return HashingEmbedder()


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CachedEmbedder:
    """Wraps an embedder with a database-backed cache.

    Attribute and subject strings repeat heavily across a collection, so this
    turns most of the work into a lookup and keeps matching cheap to re-run.
    """

    def __init__(self, embedder: Embedder, conn: sqlite3.Connection | None = None):
        self.embedder = embedder
        self.conn = conn
        self.name = embedder.name
        if conn is not None:
            conn.executescript(EMBEDDING_SCHEMA)

    def _load(self, hashes: list[str]) -> dict[str, list[float]]:
        if self.conn is None or not hashes:
            return {}
        marks = ",".join("?" * len(hashes))
        rows = self.conn.execute(
            f"SELECT text_hash, vector FROM embeddings "
            f"WHERE model = ? AND text_hash IN ({marks})",
            [self.name, *hashes],
        )
        return {row["text_hash"]: json.loads(row["vector"]) for row in rows}

    def _store(self, pairs: list[tuple[str, list[float]]]) -> None:
        if self.conn is None or not pairs:
            return
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.conn.executemany(
            "INSERT OR REPLACE INTO embeddings (text_hash, model, vector, created_at) "
            "VALUES (?, ?, ?, ?)",
            [(digest, self.name, json.dumps(vector), now) for digest, vector in pairs],
        )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        digests = [_hash(text) for text in texts]
        cached = self._load(list(dict.fromkeys(digests)))

        missing = [
            text for text, digest in zip(texts, digests) if digest not in cached
        ]
        if missing:
            unique = list(dict.fromkeys(missing))
            fresh = self.embedder.embed(unique)
            new_pairs = [(_hash(text), vector) for text, vector in zip(unique, fresh)]
            cached.update({digest: vector for digest, vector in new_pairs})
            self._store(new_pairs)

        return [cached[digest] for digest in digests]
