"""Dependency-free local embeddings (feature hashing).

Word unigrams, word bigrams and character trigrams are hashed into a fixed-size
signed vector and L2-normalised. It captures lexical overlap (including across
Hindi/English spelling variants via character n-grams), not deep semantics;
configure an Ollama or OpenAI-compatible embedding model for semantic recall.
"""

from __future__ import annotations

import hashlib
import math
import re

from ...types import ProviderTier
from ..base import EmbeddingProvider, Readiness

_WORD = re.compile(r"[\wऀ-ॿ]+", re.UNICODE)


def _bucket(feature: str, dim: int) -> tuple[int, float]:
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "little")
    return value % dim, 1.0 if (value >> 63) & 1 else -1.0


def embed_text(text: str, dim: int = 384) -> list[float]:
    vector = [0.0] * dim
    words = _WORD.findall(text.lower())
    features: list[tuple[str, float]] = [(f"w:{w}", 1.0) for w in words]
    features += [(f"b:{a}_{b}", 0.7) for a, b in zip(words, words[1:], strict=False)]
    for word in words:
        padded = f"^{word}$"
        features += [(f"c:{padded[i:i + 3]}", 0.4) for i in range(len(padded) - 2)]
    for feature, weight in features:
        index, sign = _bucket(feature, dim)
        vector[index] += sign * weight
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector] if norm else vector


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


class HashingEmbedding(EmbeddingProvider):
    name = "hashing"
    tier = ProviderTier.LOCAL
    model = "feature-hash-384"
    dim = 384
    description = "built-in lexical embeddings (no model download, no network)"

    def readiness(self) -> Readiness:
        return Readiness.ready()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [embed_text(text, self.dim) for text in texts]
