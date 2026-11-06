"""Short-lived HMAC task tickets.

A ticket is the only credential a sandbox job receives from the orchestrator. It lets the job read
its own task bundle and post its own result, nothing else. GitLab write tokens never leave the orchestrator.
"""
from __future__ import annotations

import hashlib
import hmac
import time


def issue(secret: str, task_id: str, ttl_seconds: int, now: float | None = None) -> str:
    expires = int((now or time.time()) + ttl_seconds)
    payload = f"{task_id}.{expires}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify(secret: str, ticket: str, task_id: str, now: float | None = None) -> bool:
    try:
        ticket_task, expires, sig = ticket.rsplit(".", 2)
    except ValueError:
        return False
    expected = hmac.new(secret.encode(), f"{ticket_task}.{expires}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    if ticket_task != task_id:
        return False
    try:
        return int(expires) >= int(now or time.time())
    except ValueError:
        return False
