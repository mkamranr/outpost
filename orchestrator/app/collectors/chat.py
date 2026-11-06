"""Chat context: a short summary of whatever the conversation is attached to (issue, MR or commit)."""
from __future__ import annotations

from typing import Any

from . import CollectContext, collector, truncate


@collector("subject")
def subject(ctx: CollectContext) -> dict[str, Any]:
    t = ctx.target
    kind = t.get("noteable_type")
    if kind == "issue":
        data = ctx.gl.issue(t["project_id"], t["iid"])
    elif kind == "merge_request":
        data = ctx.gl.merge_request(t["project_id"], t["iid"])
    else:
        return {"kind": kind or "commit", "ref": ctx.ref, "sha": ctx.sha}
    out = {k: data.get(k) for k in ("iid", "title", "state", "labels", "source_branch", "target_branch") if data.get(k)}
    pipe = data.get("head_pipeline") or {}
    if pipe:
        out["head_pipeline"] = {"id": pipe.get("id"), "status": pipe.get("status")}
    return {"kind": kind, **out, "description": truncate(data.get("description") or "", ctx.budget_tokens, keep="head")}
