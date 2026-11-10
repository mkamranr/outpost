"""Context collectors: each gathers one focused slice of context for an agent, within a token budget.

A 27B model reasons better on 20-40K tokens of focused context than on a full window, so every
collector trims hard and says what it dropped.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
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
    instruction: str = ""  # the text after the command, e.g. a ref range for release notes
    store: Any = None  # the TaskStore, for collectors that read what the platform learned (review memory)
    agent: str = ""  # the agent the context is for


Collector = Callable[[CollectContext], Any]
COLLECTORS: dict[str, Collector] = {}
# Collectors with a small fixed budget, taken off the top so they don't halve everyone else's share.
FIXED_BUDGETS: dict[str, int] = {}


def collector(name: str, budget: int | None = None) -> Callable[[Collector], Collector]:
    def wrap(fn: Collector) -> Collector:
        COLLECTORS[name] = fn
        if budget is not None:
            FIXED_BUDGETS[name] = budget
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
    fixed = {n: min(FIXED_BUDGETS[n], ctx.budget_tokens // 4) for n in names if n in FIXED_BUDGETS}
    shared = [n for n in names if n not in fixed]
    share = max((ctx.budget_tokens - sum(fixed.values())) // max(len(shared), 1), 500)
    out: dict[str, Any] = {}
    for name in names:
        fn = COLLECTORS.get(name)
        if fn is None:
            raise KeyError(f"unknown collector {name}")
        sub = replace(ctx, budget_tokens=fixed.get(name, share))
        try:
            out[name] = fn(sub)
        except Exception as exc:  # noqa: BLE001 - context is best effort
            out[name] = {"error": f"{type(exc).__name__}: {exc}"[:500]}
    return out


from . import chat, ci, issues, merge_requests, releases, security  # noqa: E402,F401  (register collectors)
