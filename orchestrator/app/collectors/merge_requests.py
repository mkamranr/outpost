"""Merge request context: metadata, the diff under review, and the thread an instruction came from."""
from __future__ import annotations

from typing import Any

from . import CollectContext, collector, truncate
from .ci import _format_diffs


@collector("mr")
def mr(ctx: CollectContext) -> dict[str, Any]:
    data = ctx.gl.merge_request(ctx.target["project_id"], ctx.target["iid"])
    return {
        "iid": data["iid"],
        "title": data["title"],
        "source_branch": data["source_branch"],
        "target_branch": data["target_branch"],
        "labels": data.get("labels", []),
        "description": truncate(data.get("description") or "", ctx.budget_tokens, keep="head"),
    }


@collector("mr_diff")
def mr_diff(ctx: CollectContext) -> dict[str, Any]:
    diffs = ctx.gl.merge_request_diffs(ctx.target["project_id"], ctx.target["iid"])
    return {"files": [d.get("new_path") for d in diffs], "diff": _format_diffs(diffs, ctx.budget_tokens)}


@collector("discussion")
def discussion(ctx: CollectContext) -> dict[str, Any]:
    """The thread containing the triggering comment, including the code line it is attached to."""
    t = ctx.target
    if not t.get("discussion_id"):
        return {"notes": [], "position": None}
    data = ctx.gl.discussion(t["project_id"], t["noteable_type"], t["iid"], t["discussion_id"])
    notes = [f"@{n['author']['username']}: {n['body']}" for n in data.get("notes", []) if not n.get("system")]
    position = t.get("note_position") or next((n.get("position") for n in data.get("notes", []) if n.get("position")), None)
    pos = None
    if position:
        pos = {
            "path": position.get("new_path") or position.get("old_path"),
            "new_line": position.get("new_line"),
            "old_line": position.get("old_line"),
        }
    return {"notes": [truncate(n, ctx.budget_tokens // max(len(notes), 1)) for n in notes], "position": pos}
