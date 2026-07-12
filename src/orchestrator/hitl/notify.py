"""Reviewer notification: always logged, optionally posted to a
Slack-compatible webhook. Notification failures never block escalation."""

from __future__ import annotations

import logging

import httpx

from orchestrator.config import get_settings

logger = logging.getLogger(__name__)


def notify_approval(approval: dict, webhook_url: str | None = None) -> None:
    logger.info(
        "[approval %s] task=%s trigger=%s level=%s — %s",
        approval.get("id"),
        approval.get("task_id"),
        approval.get("trigger"),
        approval.get("level"),
        approval.get("reasoning"),
    )
    url = webhook_url if webhook_url is not None else get_settings().approval_webhook_url
    if not url:
        return
    text = (
        f"Approval needed: task {approval.get('task_id')} — {approval.get('trigger')} "
        f"({approval.get('level')}). {approval.get('reasoning') or ''}"
    )
    try:
        httpx.post(url, json={"text": text}, timeout=5).raise_for_status()
    except Exception as error:
        logger.warning("Approval webhook failed: %s", error)
