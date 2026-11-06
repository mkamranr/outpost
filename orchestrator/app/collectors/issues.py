"""Issue context: the issue itself, its discussion, and the latest approved plan."""
from __future__ import annotations

from typing import Any

from . import CollectContext, collector, truncate

PLAN_MARKER = "<!-- agent:plan -->"


@collector("issue")
def issue(ctx: CollectContext) -> dict[str, Any]:
    data = ctx.gl.issue(ctx.target["project_id"], ctx.target["iid"])
    return {
        "iid": data["iid"],
        "title": data["title"],
        "labels": data.get("labels", []),
        "description": truncate(data.get("description") or "", ctx.budget_tokens, keep="head"),
    }


@collector("issue_notes")
def issue_notes(ctx: CollectContext) -> dict[str, Any]:
    notes = [n for n in ctx.gl.notes(ctx.target["project_id"], "issue", ctx.target["iid"]) if not n.get("system")]
    # Only trust plans the bot itself posted; anyone can paste the marker into a comment.
    plan = next((n["body"] for n in reversed(notes)
                 if PLAN_MARKER in (n.get("body") or "") and n["author"]["username"] == ctx.bot_username), None)
    discussion = "\n\n".join(
        f"@{n['author']['username']}: {n['body']}" for n in notes if PLAN_MARKER not in (n.get("body") or "")
    )
    plan_budget = ctx.budget_tokens // 2 if plan else 0
    return {
        "plan": truncate(plan, plan_budget, keep="head") if plan else None,
        "discussion": truncate(discussion, ctx.budget_tokens - plan_budget, keep="tail"),
    }
