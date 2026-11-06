"""Read-only GitLab tools: issues, merge requests, pipelines and repository files of the task's project."""
from __future__ import annotations

from typing import Any

from ..collectors.ci import _format_diffs, focus_trace
from . import ToolContext, tool

_INT = {"type": "integer"}
_STR = {"type": "string"}


def _note(n: dict[str, Any]) -> str:
    return f"@{n['author']['username']} ({n.get('created_at', '')[:10]}): {n.get('body', '')}"


def _brief(item: dict[str, Any]) -> dict[str, Any]:
    keys = ("iid", "title", "state", "labels", "source_branch", "target_branch", "web_url", "updated_at")
    out = {k: item[k] for k in keys if k in item}
    if item.get("author"):
        out["author"] = item["author"].get("username")
    return out


# -- issues ---------------------------------------------------------------------------

@tool("get_issue", "Get an issue of this project: title, state, labels, author and description.",
      {"iid": _INT}, ["iid"])
def get_issue(ctx: ToolContext, iid: int) -> dict[str, Any]:
    data = ctx.gl.issue(ctx.project_id, int(iid))
    return {**_brief(data), "description": data.get("description") or ""}


@tool("list_issue_notes", "List the comments on an issue, oldest first (system notes omitted).", {"iid": _INT}, ["iid"])
def list_issue_notes(ctx: ToolContext, iid: int) -> str:
    notes = [n for n in ctx.gl.notes(ctx.project_id, "issue", int(iid)) if not n.get("system")]
    return "\n\n".join(_note(n) for n in notes) or "no comments"


@tool("search_issues", "Search this project's issues by text. state is opened, closed or all.",
      {"query": _STR, "state": {"type": "string", "enum": ["opened", "closed", "all"], "default": "all"}}, ["query"])
def search_issues(ctx: ToolContext, query: str, state: str = "all") -> list[dict[str, Any]]:
    return [_brief(i) for i in ctx.gl.issues(ctx.project_id, search=str(query), state=state, per_page=20)]


# -- merge requests -------------------------------------------------------------------------

@tool("get_merge_request", "Get a merge request: title, branches, state, labels, head pipeline and description.",
      {"iid": _INT}, ["iid"])
def get_merge_request(ctx: ToolContext, iid: int) -> dict[str, Any]:
    data = ctx.gl.merge_request(ctx.project_id, int(iid))
    pipe = data.get("head_pipeline") or {}
    return {**_brief(data), "sha": data.get("sha"), "head_pipeline": {k: pipe.get(k) for k in ("id", "status")} if pipe else None,
            "description": data.get("description") or ""}


@tool("get_mr_diff", "Get the diff of a merge request, optionally only for one file path.",
      {"iid": _INT, "path": _STR}, ["iid"])
def get_mr_diff(ctx: ToolContext, iid: int, path: str | None = None) -> str:
    diffs = ctx.gl.merge_request_diffs(ctx.project_id, int(iid))
    if path:
        diffs = [d for d in diffs if path in (d.get("new_path"), d.get("old_path"))]
    return _format_diffs(diffs, ctx.budget_tokens) or "no changes"


@tool("list_mr_discussions", "List the discussion threads on a merge request, with the file and line they are attached to.",
      {"iid": _INT}, ["iid"])
def list_mr_discussions(ctx: ToolContext, iid: int) -> str:
    threads = []
    for d in ctx.gl.discussions(ctx.project_id, "merge_request", int(iid)):
        notes = [n for n in d.get("notes", []) if not n.get("system")]
        if not notes:
            continue
        pos = notes[0].get("position") or {}
        where = f" on `{pos.get('new_path') or pos.get('old_path')}:{pos.get('new_line') or pos.get('old_line')}`" if pos else ""
        resolved = " (resolved)" if notes[0].get("resolved") else ""
        threads.append(f"### Thread{where}{resolved}\n" + "\n".join(_note(n) for n in notes))
    return "\n\n".join(threads) or "no discussions"


@tool("search_merge_requests", "Search this project's merge requests by text. state is opened, merged, closed or all.",
      {"query": _STR, "state": {"type": "string", "enum": ["opened", "merged", "closed", "all"], "default": "all"}}, ["query"])
def search_merge_requests(ctx: ToolContext, query: str, state: str = "all") -> list[dict[str, Any]]:
    return [_brief(m) for m in ctx.gl.merge_requests(ctx.project_id, search=str(query), state=state, per_page=20)]


# -- CI ----------------------------------------------------------------------------------------

@tool("list_pipelines", "List recent pipelines, newest first. Filter by ref (branch) and/or status (e.g. failed).",
      {"ref": _STR, "status": _STR})
def list_pipelines(ctx: ToolContext, ref: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"per_page": 15, "order_by": "id", "sort": "desc"}
    if ref:
        params["ref"] = ref
    if status:
        params["status"] = status
    keys = ("id", "status", "ref", "sha", "source", "created_at", "web_url")
    return [{k: p.get(k) for k in keys} for p in ctx.gl.pipelines(ctx.project_id, **params)]


@tool("get_pipeline_jobs", "List the jobs of a pipeline with stage, status and failure reason.",
      {"pipeline_id": _INT}, ["pipeline_id"])
def get_pipeline_jobs(ctx: ToolContext, pipeline_id: int) -> list[dict[str, Any]]:
    keys = ("id", "name", "stage", "status", "failure_reason", "allow_failure")
    return [{k: j.get(k) for k in keys} for j in ctx.gl.pipeline_jobs(ctx.project_id, int(pipeline_id))]


@tool("get_job_log", "Get a CI job's log, focused on the end and on lines that look like errors.",
      {"job_id": _INT}, ["job_id"])
def get_job_log(ctx: ToolContext, job_id: int) -> str:
    return focus_trace(ctx.gl.job_trace(ctx.project_id, int(job_id)), ctx.budget_tokens) or "empty log"


@tool("lint_ci", "Validate the project's .gitlab-ci.yml at a ref (default: the task's commit). Returns errors and warnings.",
      {"ref": _STR})
def lint_ci(ctx: ToolContext, ref: str | None = None) -> dict[str, Any]:
    lint = ctx.gl.ci_lint_ref(ctx.project_id, ref or ctx.ref)
    return {k: lint.get(k) for k in ("valid", "errors", "warnings")}


# -- repository ----------------------------------------------------------------------------------

@tool("get_file", "Read a file from the repository at a ref (default: the task's commit), optionally a line range.",
      {"path": _STR, "ref": _STR, "start_line": _INT, "end_line": _INT}, ["path"])
def get_file(ctx: ToolContext, path: str, ref: str | None = None, start_line: int | None = None,
             end_line: int | None = None) -> str:
    text = ctx.gl.raw_file(ctx.project_id, str(path).lstrip("/"), ref or ctx.ref)
    if text is None:
        return f"error: {path} not found"
    lines = text.splitlines()
    start = max(1, int(start_line or 1))
    end = min(len(lines), int(end_line or start + 399))
    body = "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))
    return body + (f"\n[file has {len(lines)} lines]" if end < len(lines) else "")


@tool("list_tree", "List files and directories under a repository path (default: root).",
      {"path": _STR, "ref": _STR, "recursive": {"type": "boolean", "default": False}})
def list_tree(ctx: ToolContext, path: str = "", ref: str | None = None, recursive: bool = False) -> str:
    entries = ctx.gl.repository_tree(ctx.project_id, str(path or "").strip("/"), ref or ctx.ref, bool(recursive))
    lines = [e["path"] + ("/" if e.get("type") == "tree" else "") for e in entries]
    more = f"\n[... {len(lines) - 400} more ...]" if len(lines) > 400 else ""
    return "\n".join(lines[:400]) + more or "empty"


@tool("search_code", "Search the repository's code for text (default branch). Returns file, line and snippet.",
      {"query": _STR}, ["query"])
def search_code(ctx: ToolContext, query: str) -> str:
    hits = ctx.gl.search(ctx.project_id, "blobs", str(query))
    parts = [f"{h.get('path')}:{h.get('startline')}\n{h.get('data', '').rstrip()}" for h in hits[:30]]
    return "\n\n".join(parts) or "no matches"
