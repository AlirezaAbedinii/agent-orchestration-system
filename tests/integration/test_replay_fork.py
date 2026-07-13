"""Replay acceptance: a strict replay reproduces the original step-for-step
with zero API calls; a fork at step k replays the prefix, applies the human
modification, diverges afterwards, and the comparison shows exactly that."""

REQUEST = (
    "Compare open-source vector databases: gather facts about Chroma from the web, "
    "compute the GitHub star ranking from the demo database, generate a comparison "
    "table using Python, and write a comparison memo saved as memo.md."
)


def _run_original(client) -> dict:
    task_id = client.post("/tasks", json={"request": REQUEST}).json()["task_id"]
    bundle = client.get(f"/tasks/{task_id}").json()
    assert bundle["status"] == "completed"
    return bundle


def test_strict_replay_reproduces_original_with_zero_api_calls(client):
    original = _run_original(client)
    original_id = original["task_id"]

    launched = client.post(f"/replay/{original_id}", json={}).json()
    assert launched["mode"] == "replay"
    replay_id = launched["replay_task_id"]

    replay = client.get(f"/tasks/{replay_id}").json()
    assert replay["status"] == "completed"
    assert replay["replay_of"] == original_id

    # reproduces the original step-for-step
    assert replay["final_output"] == original["final_output"]
    assert {s["sid"]: s["output"] for s in replay["subtasks"]} == {
        s["sid"]: s["output"] for s in original["subtasks"]
    }

    # zero API calls: every response came from the recording (there is no
    # fallback client in strict mode, so nothing else was even reachable)
    replay_calls = client.get(f"/replay/{replay_id}/steps").json()["steps"]
    assert replay_calls
    assert all(call["model"].startswith("replay:") for call in replay_calls)
    assert client.get(f"/traces/{replay_id}/costs").json()["total_usd"] == 0.0


def test_fork_at_step_k_replays_prefix_and_diverges_after(client):
    original = _run_original(client)
    original_id = original["task_id"]

    steps = client.get(f"/replay/{original_id}/steps").json()["steps"]
    synth_index = next(
        i for i, call in enumerate(steps)
        if call["agent"] == "supervisor" and "Synthesize the final deliverable" in call["prompt"]
    )
    modified_text = "FINAL-FORKED: memo rewritten by the reviewer during replay"

    launched = client.post(
        f"/replay/{original_id}",
        json={"llm_call_id": steps[synth_index]["id"], "response_text": modified_text},
    ).json()
    assert launched["mode"] == "fork"
    fork_id = launched["replay_task_id"]

    fork = client.get(f"/tasks/{fork_id}").json()
    assert fork["status"] == "completed"
    assert fork["final_output"] == modified_text  # step k took the human's response

    # the fork's calls: everything before k replayed, step k is the override,
    # and nothing ran after it (extraction is disabled during replays)
    fork_calls = client.get(f"/replay/{fork_id}/steps").json()["steps"]
    models = [call["model"] for call in fork_calls]
    assert len(fork_calls) == synth_index + 1
    assert models.count("replay:modified") == 1
    assert all(model.startswith("replay:") for model in models)

    # side-by-side comparison: steps < k match, step k diverges
    comparison = client.get(f"/replay/{fork_id}/compare").json()
    assert comparison["original_task_id"] == original_id
    paired = [s for s in comparison["steps"] if s["original"] and s["fork"]]
    synth_step = next(
        s for s in paired if s["fork"]["model"] == "replay:modified"
    )
    assert synth_step["diverged"] is True
    prefix = [s for s in paired if s is not synth_step]
    assert prefix and all(not s["diverged"] for s in prefix)

    assert comparison["final_output"]["diverged"] is True
    assert comparison["final_output"]["fork"] == modified_text
    # subtask outputs were replayed identically
    assert all(not diff["diverged"] for diff in comparison["subtasks"].values())


def test_fork_requires_matching_call_and_paired_fields(client):
    original = _run_original(client)
    original_id = original["task_id"]

    # unknown llm_call_id -> 404
    response = client.post(
        f"/replay/{original_id}", json={"llm_call_id": "nope", "response_text": "x"}
    )
    assert response.status_code == 404

    # one field without the other -> validation error
    response = client.post(f"/replay/{original_id}", json={"llm_call_id": "abc"})
    assert response.status_code == 422

    # compare on a non-replay task -> 400
    assert client.get(f"/replay/{original_id}/compare").status_code == 400
