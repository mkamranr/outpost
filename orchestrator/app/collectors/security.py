"""Security scanner findings (SAST, dependency and secret detection) for the security remediator."""
from __future__ import annotations

from typing import Any

import yaml

from ..config import SEVERITIES
from . import CollectContext, collector, truncate

IGNORE_FILE = ".gitlab/agent-security-ignore.yml"
MAX_FINDINGS = 10
SNIPPET_LINES = 6


def ignored_ids(text: str | None) -> set[str]:
    """`.gitlab/agent-security-ignore.yml`: a list of finding ids or identifiers (CVE-..., CWE-...), as strings
    or as {id: ..., reason: ...}. Humans maintain it; agents cannot change it (it is always protected)."""
    try:
        data = yaml.safe_load(text or "") or []
    except yaml.YAMLError:
        return set()
    items = data.get("ignore", []) if isinstance(data, dict) else data
    out = set()
    for item in items if isinstance(items, list) else []:
        value = item.get("id") if isinstance(item, dict) else item
        if value:
            out.add(str(value))
    return out


def _snippet(ctx: CollectContext, path: str, line: int | None, files: dict[str, str | None]) -> str | None:
    if not line or not path:
        return None
    if path not in files:
        files[path] = ctx.gl.raw_file(ctx.target["project_id"], path, ctx.sha)
    text = files[path]
    if text is None:
        return None
    lines = text.splitlines()
    start, end = max(line - SNIPPET_LINES, 1), min(line + SNIPPET_LINES, len(lines))
    return "\n".join(f"{n:>5} {'>' if n == line else ' '} {lines[n - 1]}" for n in range(start, end + 1))


def pipeline_for(ctx: CollectContext) -> tuple[int | None, list[str] | None]:
    """(pipeline id, files the MR changes or None). MRs use their head pipeline."""
    t = ctx.target
    if t.get("kind") == "pipeline" and t.get("pipeline_id"):
        return t["pipeline_id"], None
    if t.get("noteable_type") == "merge_request" and t.get("iid"):
        mr = ctx.gl.merge_request(t["project_id"], t["iid"])
        changed = [d.get("new_path") for d in ctx.gl.merge_request_diffs(t["project_id"], t["iid"])]
        return (mr.get("head_pipeline") or {}).get("id"), changed
    return None, None


@collector("vulnerabilities")
def vulnerabilities(ctx: CollectContext) -> dict[str, Any]:
    pid = ctx.target["project_id"]
    policy = ctx.project.security_remediator
    pipeline_id, changed = pipeline_for(ctx)
    if pipeline_id is None:
        return {"pipeline_id": None, "findings": [], "note": "no pipeline to read security reports from"}
    project = ctx.gl.project(pid)
    ignore = ignored_ids(ctx.gl.raw_file(pid, IGNORE_FILE, project.get("default_branch") or ctx.ref))
    findings = ctx.gl.security_findings(pid, pipeline_id)
    total = len(findings)
    kept = []
    for f in findings:
        if not policy.wanted(f["severity"]):
            continue
        if f.get("id") in ignore or ignore & set(f.get("identifiers") or []):
            continue
        # On an MR, fix what the MR touches; findings elsewhere predate it and belong to the default branch.
        if changed is not None and f.get("file") not in changed:
            continue
        kept.append(f)
    kept.sort(key=lambda f: (SEVERITIES.index(f["severity"]) if f["severity"] in SEVERITIES else 99,
                             f.get("file") or "", f.get("line") or 0))
    shown = kept[:MAX_FINDINGS]
    files: dict[str, str | None] = {}
    share = max(ctx.budget_tokens // max(len(shown), 1), 300)
    for f in shown:
        if f.get("description"):
            f["description"] = truncate(f["description"], share // 3, keep="head")
        snippet = _snippet(ctx, f.get("file") or "", f.get("line"), files)
        if snippet:
            f["code"] = truncate(snippet, share // 2, keep="head")
    return {
        "pipeline_id": pipeline_id,
        "min_severity": policy.min_severity,
        "total_findings": total,
        "matching": len(kept),
        "findings": shown,
        "scope": "files changed by the merge request" if changed is not None else "whole repository",
    }
