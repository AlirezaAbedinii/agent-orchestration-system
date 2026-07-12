"""Long-term semantic memory in ChromaDB.

Three collections — episodes (what was asked and what approach worked),
facts (domain facts discovered), preferences (user preferences observed) —
each entry carrying user_id, task_id, created_at, access_count, and an
importance score. Embeddings are computed client-side (memory/embeddings.py)
and passed explicitly.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from orchestrator.config import get_settings
from orchestrator.memory.embeddings import embed_texts
from orchestrator.memory.management import compute_importance

KINDS = ("episodes", "facts", "preferences")


@dataclass
class MemoryHit:
    id: str
    text: str
    kind: str
    metadata: dict
    distance: float | None = None


class LongTermMemory:
    def __init__(self, client: Any | None = None):
        if client is None:
            import chromadb

            settings = get_settings()
            client = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
        self._client = client
        self._collections: dict[str, Any] = {}

    def _col(self, kind: str):
        if kind not in KINDS:
            raise ValueError(f"Unknown memory kind {kind!r}; expected one of {KINDS}")
        if kind not in self._collections:
            self._collections[kind] = self._client.get_or_create_collection(
                name=f"memory_{kind}", metadata={"hnsw:space": "cosine"}
            )
        return self._collections[kind]

    def add(
        self,
        kind: str,
        text: str,
        *,
        user_id: str,
        task_id: str | None = None,
        extra: dict | None = None,
    ) -> str:
        memory_id = uuid.uuid4().hex
        now = time.time()
        metadata = {
            "kind": kind,
            "user_id": user_id,
            "task_id": task_id or "",
            "created_at": now,
            "last_accessed_at": now,
            "access_count": 0,
            "importance": compute_importance(0, now, now=now),
        }
        if extra:
            metadata.update(extra)
        self._col(kind).add(
            ids=[memory_id], documents=[text], embeddings=embed_texts([text]), metadatas=[metadata]
        )
        return memory_id

    def query(self, kind: str, text: str, *, user_id: str, k: int = 3) -> list[MemoryHit]:
        collection = self._col(kind)
        if collection.count() == 0:
            return []
        result = collection.query(
            query_embeddings=embed_texts([text]),
            n_results=max(1, k),
            where={"user_id": user_id},
            include=["documents", "metadatas", "distances"],
        )
        return [
            MemoryHit(id=mid, text=doc, kind=kind, metadata=meta, distance=dist)
            for mid, doc, meta, dist in zip(
                result["ids"][0],
                result["documents"][0],
                result["metadatas"][0],
                result["distances"][0],
            )
        ]

    def get_all(self, user_id: str) -> dict[str, list[dict]]:
        grouped: dict[str, list[dict]] = {}
        for kind in KINDS:
            result = self._col(kind).get(
                where={"user_id": user_id}, include=["documents", "metadatas"]
            )
            items = [
                {
                    "id": mid,
                    "text": doc,
                    "task_id": meta.get("task_id"),
                    "importance": meta.get("importance"),
                    "access_count": meta.get("access_count"),
                    "created_at": meta.get("created_at"),
                    "last_accessed_at": meta.get("last_accessed_at"),
                }
                for mid, doc, meta in zip(result["ids"], result["documents"], result["metadatas"])
            ]
            grouped[kind] = sorted(items, key=lambda item: item["importance"] or 0, reverse=True)
        return grouped

    def all_items(self, kind: str, user_id: str | None = None, with_embeddings: bool = False):
        include = ["documents", "metadatas"] + (["embeddings"] if with_embeddings else [])
        where = {"user_id": user_id} if user_id else None
        return self._col(kind).get(where=where, include=include)

    def bump_access(self, kind: str, ids: list[str]) -> None:
        if not ids:
            return
        collection = self._col(kind)
        result = collection.get(ids=list(ids), include=["metadatas"])
        now = time.time()
        updated = []
        for metadata in result["metadatas"]:
            count = int(metadata.get("access_count", 0)) + 1
            metadata.update(
                access_count=count,
                last_accessed_at=now,
                importance=compute_importance(count, now, now=now),
            )
            updated.append(metadata)
        collection.update(ids=result["ids"], metadatas=updated)

    def set_metadata(self, kind: str, ids: list[str], metadatas: list[dict]) -> None:
        self._col(kind).update(ids=ids, metadatas=metadatas)

    def delete(self, kind: str, ids: list[str]) -> None:
        if ids:
            self._col(kind).delete(ids=list(ids))

    def delete_user(self, user_id: str) -> dict[str, int]:
        counts = {}
        for kind in KINDS:
            result = self._col(kind).get(where={"user_id": user_id})
            ids = result["ids"]
            if ids:
                self._col(kind).delete(ids=ids)
            counts[kind] = len(ids)
        return counts
