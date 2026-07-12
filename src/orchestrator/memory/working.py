"""Task-scoped working memory shared by all agents during one execution.

Backed by Redis (one hash per task plus an error list); every agent reads and
writes through this module. Memory is scoped to a single task: it is cleared
when the task completes, and a TTL guards against leaks from crashed runs.
`InMemoryWorkingMemory` is a drop-in used by unit tests.
"""

from __future__ import annotations

import json
from typing import Any

import redis

from orchestrator.config import get_settings


class WorkingMemory:
    def __init__(self, client: redis.Redis | None = None):
        self._redis = client or redis.Redis.from_url(
            get_settings().redis_url, decode_responses=True
        )

    @staticmethod
    def _key(task_id: str) -> str:
        return f"task:{task_id}:working"

    @staticmethod
    def _errors_key(task_id: str) -> str:
        return f"task:{task_id}:errors"

    def _touch(self, task_id: str) -> None:
        ttl = get_settings().working_memory_ttl_s
        self._redis.expire(self._key(task_id), ttl)
        self._redis.expire(self._errors_key(task_id), ttl)

    def start(self, task_id: str, user_id: str) -> None:
        self._redis.hset(self._key(task_id), mapping={"user_id": user_id})
        self._touch(task_id)

    def set_plan(self, task_id: str, plan: dict) -> None:
        self._redis.hset(self._key(task_id), "plan", json.dumps(plan))
        self._touch(task_id)

    def get_plan(self, task_id: str) -> dict | None:
        raw = self._redis.hget(self._key(task_id), "plan")
        return json.loads(raw) if raw else None

    def record_subtask_output(self, task_id: str, sid: str, output: str) -> None:
        self._redis.hset(self._key(task_id), f"subtask:{sid}", output)
        self._touch(task_id)

    def get_subtask_outputs(self, task_id: str) -> dict[str, str]:
        entries = self._redis.hgetall(self._key(task_id))
        return {
            field.removeprefix("subtask:"): value
            for field, value in entries.items()
            if field.startswith("subtask:")
        }

    def set_intermediate(self, task_id: str, name: str, value: Any) -> None:
        self._redis.hset(self._key(task_id), f"intermediate:{name}", json.dumps(value))
        self._touch(task_id)

    def get_intermediates(self, task_id: str) -> dict[str, Any]:
        entries = self._redis.hgetall(self._key(task_id))
        return {
            field.removeprefix("intermediate:"): json.loads(value)
            for field, value in entries.items()
            if field.startswith("intermediate:")
        }

    def record_error(self, task_id: str, sid: str, error: str) -> None:
        self._redis.rpush(self._errors_key(task_id), json.dumps({"sid": sid, "error": error}))
        self._touch(task_id)

    def get_errors(self, task_id: str) -> list[dict]:
        return [json.loads(raw) for raw in self._redis.lrange(self._errors_key(task_id), 0, -1)]

    def get_user(self, task_id: str) -> str | None:
        return self._redis.hget(self._key(task_id), "user_id")

    def snapshot(self, task_id: str) -> dict:
        return {
            "fields": self._redis.hgetall(self._key(task_id)),
            "errors": self.get_errors(task_id),
        }

    def exists(self, task_id: str) -> bool:
        return bool(self._redis.exists(self._key(task_id)))

    def clear(self, task_id: str) -> None:
        self._redis.delete(self._key(task_id), self._errors_key(task_id))

    def clear_user(self, user_id: str) -> int:
        """Delete working memory for every task belonging to a user."""
        cleared = 0
        for key in self._redis.scan_iter(match="task:*:working"):
            if self._redis.hget(key, "user_id") == user_id:
                task_id = key.split(":", 2)[1]
                self.clear(task_id)
                cleared += 1
        return cleared


class InMemoryWorkingMemory:
    """Dict-backed drop-in for unit tests (same behaviour, no Redis)."""

    def __init__(self):
        self._fields: dict[str, dict[str, str]] = {}
        self._errors: dict[str, list[dict]] = {}

    def start(self, task_id: str, user_id: str) -> None:
        self._fields.setdefault(task_id, {})["user_id"] = user_id

    def set_plan(self, task_id: str, plan: dict) -> None:
        self._fields.setdefault(task_id, {})["plan"] = json.dumps(plan)

    def get_plan(self, task_id: str) -> dict | None:
        raw = self._fields.get(task_id, {}).get("plan")
        return json.loads(raw) if raw else None

    def record_subtask_output(self, task_id: str, sid: str, output: str) -> None:
        self._fields.setdefault(task_id, {})[f"subtask:{sid}"] = output

    def get_subtask_outputs(self, task_id: str) -> dict[str, str]:
        return {
            field.removeprefix("subtask:"): value
            for field, value in self._fields.get(task_id, {}).items()
            if field.startswith("subtask:")
        }

    def set_intermediate(self, task_id: str, name: str, value: Any) -> None:
        self._fields.setdefault(task_id, {})[f"intermediate:{name}"] = json.dumps(value)

    def get_intermediates(self, task_id: str) -> dict[str, Any]:
        return {
            field.removeprefix("intermediate:"): json.loads(value)
            for field, value in self._fields.get(task_id, {}).items()
            if field.startswith("intermediate:")
        }

    def record_error(self, task_id: str, sid: str, error: str) -> None:
        self._errors.setdefault(task_id, []).append({"sid": sid, "error": error})

    def get_errors(self, task_id: str) -> list[dict]:
        return list(self._errors.get(task_id, []))

    def get_user(self, task_id: str) -> str | None:
        return self._fields.get(task_id, {}).get("user_id")

    def snapshot(self, task_id: str) -> dict:
        return {"fields": dict(self._fields.get(task_id, {})), "errors": self.get_errors(task_id)}

    def exists(self, task_id: str) -> bool:
        return task_id in self._fields

    def clear(self, task_id: str) -> None:
        self._fields.pop(task_id, None)
        self._errors.pop(task_id, None)

    def clear_user(self, user_id: str) -> int:
        task_ids = [t for t, f in self._fields.items() if f.get("user_id") == user_id]
        for task_id in task_ids:
            self.clear(task_id)
        return len(task_ids)
