"""Importance scoring, access bumping, and expiration (in-process Chroma)."""

import time

import chromadb
import pytest

from orchestrator.memory.longterm import LongTermMemory
from orchestrator.memory.management import compute_importance, expire

DAY = 86_400


@pytest.fixture()
def longterm(tmp_path):
    # PersistentClient with a per-test directory: EphemeralClient is cached by
    # settings and would share collections across tests.
    return LongTermMemory(client=chromadb.PersistentClient(path=str(tmp_path / "chroma")))


def test_more_accesses_mean_higher_importance():
    now = time.time()
    assert compute_importance(5, now, now=now) > compute_importance(1, now, now=now)


def test_staleness_decays_importance():
    now = time.time()
    fresh = compute_importance(0, now, now=now)
    half_life_old = compute_importance(0, now - 14 * DAY, now=now)
    ancient = compute_importance(0, now - 90 * DAY, now=now)
    assert fresh == pytest.approx(1.0)
    assert half_life_old == pytest.approx(0.5, rel=0.01)
    assert ancient < half_life_old < fresh


def test_accessing_a_memory_raises_its_importance(longterm):
    memory_id = longterm.add("facts", "Chroma is a vector database", user_id="alice")
    before = longterm.get_all("alice")["facts"][0]
    assert before["access_count"] == 0

    longterm.bump_access("facts", [memory_id])

    after = longterm.get_all("alice")["facts"][0]
    assert after["access_count"] == 1
    assert after["importance"] > before["importance"]


def test_expiration_removes_stale_low_importance_memories(longterm):
    now = time.time()
    stale_id = longterm.add("facts", "Old unused fact about nothing", user_id="alice")
    fresh_id = longterm.add("facts", "Fresh fact about vector databases", user_id="alice")
    # backdate the stale memory 90 days
    longterm.set_metadata(
        "facts",
        [stale_id],
        [{
            "kind": "facts", "user_id": "alice", "task_id": "",
            "created_at": now - 90 * DAY, "last_accessed_at": now - 90 * DAY,
            "access_count": 0, "importance": 1.0,
        }],
    )

    report = expire(longterm, now=now)

    remaining = {item["id"] for item in longterm.get_all("alice")["facts"]}
    assert remaining == {fresh_id}
    assert {e["id"] for e in report["expired"]} == {stale_id}
    assert report["recomputed"] >= 1


def test_recently_accessed_memory_survives_expiration(longterm):
    now = time.time()
    memory_id = longterm.add("facts", "Frequently used fact", user_id="alice")
    # old but heavily accessed recently → importance stays above the floor
    longterm.set_metadata(
        "facts",
        [memory_id],
        [{
            "kind": "facts", "user_id": "alice", "task_id": "",
            "created_at": now - 90 * DAY, "last_accessed_at": now - 2 * DAY,
            "access_count": 6, "importance": 1.0,
        }],
    )

    report = expire(longterm, now=now)

    assert report["expired"] == []
    assert longterm.get_all("alice")["facts"][0]["id"] == memory_id
