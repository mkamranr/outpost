"""Release context: what changed between two refs (commits and the merge requests they came from)."""
from __future__ import annotations

import re
from typing import Any

from . import CollectContext, collector, truncate

_RANGE = re.compile(r"^\s*([\w./@{}^~-]+?)\s*(?:\.\.\.?\s*([\w./@{}^~-]+))?\s*$")
# `!12`, or `group/app!12` as merge commits write it; references to other projects are skipped.
_MR_REF = re.compile(r"(?:^|(?<=[\s(\[]))([\w.-]+(?:/[\w.-]+)*)?!(\d+)\b")
MAX_COMMITS = 300
MAX_MRS = 60


def parse_range(instruction: str, default_to: str) -> tuple[str, str] | None:
    """`v1.2.0..v1.3.0`, `v1.2.0...main` or just `v1.2.0` (up to the default branch)."""
    first = (instruction or "").strip().split("\n")[0].split(" ")[0]
    m = _RANGE.match(first)
    if not m:
        return None
    return m.group(1), m.group(2) or default_to


@collector("ref_range")
def ref_range(ctx: CollectContext) -> dict[str, Any]:
    pid = ctx.target["project_id"]
    project_path = ctx.target.get("project_path") or ""
    default_branch = ctx.gl.project(pid).get("default_branch") or "main"
    parsed = parse_range(ctx.instruction, default_branch)
    if parsed is None:
        return {"error": "no ref range given; usage: release-notes <from>..<to>"}
    start, end = parsed
    compare = ctx.gl.compare(pid, start, end)
    commits = (compare.get("commits") or [])[-MAX_COMMITS:]
    mr_iids: list[int] = []
    lines = []
    for c in commits:
        message = c.get("message") or c.get("title") or ""
        mr_iids += [int(i) for proj, i in _MR_REF.findall(message) if proj in ("", project_path)]
        lines.append(f"{c.get('short_id', '')} {c.get('title', '')} ({c.get('author_name', '?')})")
    mrs = []
    for iid in list(dict.fromkeys(mr_iids))[:MAX_MRS]:
        try:
            mr = ctx.gl.merge_request(pid, iid)
        except Exception:  # noqa: BLE001 - a reference to an MR in another project, or a deleted one
            continue
        mrs.append({"iid": iid, "title": mr.get("title"), "labels": mr.get("labels", []),
                    "author": (mr.get("author") or {}).get("username")})
    budget = ctx.budget_tokens
    return {
        "from": start, "to": end,
        "commit_count": len(compare.get("commits") or []),
        "commits": truncate("\n".join(lines), budget // 2, keep="tail"),
        "merge_requests": mrs,
    }
