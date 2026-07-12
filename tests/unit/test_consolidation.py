"""Consolidation merges near-duplicate memories into one summary."""

import json

import chromadb
import pytest

from orchestrator.llm.mock import MockLLMClient
from orchestrator.memory.longterm import LongTermMemory
from orchestrator.memory.management import consolidate

DUPLICATE_A = "Chroma is an open-source vector database for embeddings"
DUPLICATE_B = "Chroma is an open source vector database used for embeddings"
UNRELATED = "The quarterly finance report is due on Friday"
SUMMARY = "CONSOLIDATED: Chroma is an open-source vector database for embeddings."


@pytest.fixture()
def longterm(tmp_path):
    # PersistentClient with a per-test directory: EphemeralClient is cached by
    # settings and would share collections across tests.
    return LongTermMemory(client=chromadb.PersistentClient(path=str(tmp_path / "chroma")))


@pytest.fixture()
def llm(tmp_path):
    (tmp_path / "memory.json").write_text(
        json.dumps({"agent": "memory", "response": {"text": SUMMARY}}), encoding="utf-8"
    )
    return MockLLMClient(tmp_path)


def test_near_duplicates_merge_into_one_summary(longterm, llm):
    id_a = longterm.add("facts", DUPLICATE_A, user_id="alice")
    id_b = longterm.add("facts", DUPLICATE_B, user_id="alice")
    id_other = longterm.add("facts", UNRELATED, user_id="alice")
    longterm.bump_access("facts", [id_a])  # merged memory should inherit access counts

    report = consolidate(longterm, llm, events=None)

    assert report["clusters_merged"] == 1
    assert {d["id"] for d in report["deleted"]} == {id_a, id_b}

    facts = longterm.get_all("alice")["facts"]
    texts = {item["text"] for item in facts}
    assert texts == {SUMMARY, UNRELATED}

    merged = next(item for item in facts if item["text"] == SUMMARY)
    assert merged["access_count"] == 1  # inherited from the merged originals
    assert merged["id"] not in {id_a, id_b, id_other}


def test_memories_of_different_users_never_merge(longterm, llm):
    longterm.add("facts", DUPLICATE_A, user_id="alice")
    longterm.add("facts", DUPLICATE_B, user_id="bob")

    report = consolidate(longterm, llm, events=None)

    assert report["clusters_merged"] == 0
    assert longterm.get_all("alice")["facts"][0]["text"] == DUPLICATE_A
    assert longterm.get_all("bob")["facts"][0]["text"] == DUPLICATE_B


def test_unrelated_memories_stay_apart(longterm, llm):
    longterm.add("facts", DUPLICATE_A, user_id="alice")
    longterm.add("facts", UNRELATED, user_id="alice")

    report = consolidate(longterm, llm, events=None)

    assert report["clusters_merged"] == 0
    assert len(longterm.get_all("alice")["facts"]) == 2
