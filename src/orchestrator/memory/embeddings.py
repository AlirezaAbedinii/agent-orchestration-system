"""Embeddings for long-term memory.

Real mode uses OpenAI (`text-embedding-3-small`). Under MOCK_LLM a
deterministic token-hash embedding is used instead — similar texts share
tokens and land close in cosine space, which is exactly what the tests need,
with no network and no model download. Vectors are passed to Chroma
explicitly, so no server-side embedding configuration is involved.
"""

from __future__ import annotations

import hashlib
import math
import re
from functools import lru_cache

from orchestrator.config import get_settings


def embed_texts(texts: list[str]) -> list[list[float]]:
    settings = get_settings()
    if settings.mock_llm:
        return [_hash_embedding(text, settings.mock_embedding_dim) for text in texts]
    return _openai_embedder().embed_documents(texts)


@lru_cache(maxsize=1)
def _openai_embedder():
    from langchain_openai import OpenAIEmbeddings

    settings = get_settings()
    return OpenAIEmbeddings(model="text-embedding-3-small", api_key=settings.openai_api_key)


def _hash_embedding(text: str, dim: int) -> list[float]:
    vector = [0.0] * dim
    for token in re.findall(r"[a-z0-9]+", text.lower()):
        digest = int(hashlib.md5(token.encode()).hexdigest(), 16)  # stable across processes
        vector[digest % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]
