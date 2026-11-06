"""Context collectors: each gathers one focused slice of context for an agent, within a token budget.

A 27B model reasons better on 20-40K tokens of focused context than on a full window, so every
collector trims hard and says what it dropped.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..config import ProjectConfig
from ..gitlab_client import GitLab

# Rough chars-per-token for mixed code and logs with Qwen tokenizers; deliberately conservative.
CHARS_PER_TOKEN = 3


@dataclass
class CollectContext:
    gl: GitLab
    target: dict[str, Any]
    project: ProjectConfig
    sha: str
    ref: str
    budget_tokens: int
    bot_username: str = ""


Collector = Callable[[CollectContext], Any]
COLLECTORS: dict[str, Collector] = {}


def collector(name: str) -> Callable[[Collector], Collector]:
    def wrap(fn: Collector) -> Collector:
        COLLECTORS[name] = fn
        return fn
    return wrap


def truncate(text: str, max_tokens: int, keep: str = "tail") -> str:
    limit = max_tokens * CHARS_PER_TOKEN
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    if keep == "head":
        return text[:limit] + f"\n[... truncated {dropped} chars ...]"
    return f"[... truncated {dropped} chars ...]\n" + text[-limit:]


def collect(names: list[str], ctx: CollectContext) -> dict[str, Any]:
    """Run the named collectors, splitting the budget evenly. Collector failures are recorded, not fatal."""
    if not names:
        return {}
    share = max(ctx.budget_tokens // len(names), 500)
    out: dict[str, Any] = {}
    for name in names:
        fn = COLLECTORS.get(name)
        if fn is None:
            raise KeyError(f"unknown collector {name}")
        sub = CollectContext(ctx.gl, ctx.target, ctx.project, ctx.sha, ctx.ref, share, ctx.bot_username)
        try:
            out[name] = fn(sub)
        except Exception as exc:  # noqa: BLE001 - context is best effort
            out[name] = {"error": f"{type(exc).__name__}: {exc}"[:500]}
    return out


from . import chat, ci, issues, merge_requests  # noqa: E402,F401  (register collectors)
