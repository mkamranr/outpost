"""Pipeline-failure context: failed job traces, CI config, and the diff that broke the build."""
from __future__ import annotations

import re
from typing import Any

from . import CHARS_PER_TOKEN, CollectContext, collector, truncate

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_SECTION = re.compile(r"section_(start|end):\d+:[\w.-]+(\[[^\]]*\])?\r?")
_ERROR = re.compile(
    r"(error|failed|failure|exception|traceback|assert|fatal|panic|cannot|could not|not found|"
    r"undefined|segmentation|exit code|exit status|denied|timed? ?out|E\d{3,4}\b)",
    re.IGNORECASE,
)
# Lines that are noise in nearly every trace.
_NOISE = re.compile(r"^(Running with gitlab-runner|Preparing the|Using docker image|Getting source from|"
                    r"Fetching changes|Checking out|Skipping Git submodules|Executing \"step_script\"|"
                    r"Uploading artifacts|Cleaning up project directory)")


def clean_trace(raw: str) -> list[str]:
    lines = []
    for line in raw.replace("\r\n", "\n").split("\n"):
        line = _SECTION.sub("", _ANSI.sub("", line)).rstrip()
        # Carriage-return progress bars: keep the final state only.
        if "\r" in line:
            line = line.rsplit("\r", 1)[-1]
        if line and not _NOISE.match(line):
            lines.append(line)
    return lines


def focus_trace(raw: str, max_tokens: int, tail_lines: int = 120, window: int = 6, max_hits: int = 12) -> str:
    """Keep the end of the log (where the failing command is) plus windows around earlier error lines."""
    lines = clean_trace(raw)
    if not lines:
        return ""
    tail_start = max(0, len(lines) - tail_lines)
    keep: set[int] = set(range(tail_start, len(lines)))
    hits = [i for i, l in enumerate(lines[:tail_start]) if _ERROR.search(l)]
    for i in hits[:max_hits]:
        keep.update(range(max(0, i - window), min(tail_start, i + window + 1)))
    out: list[str] = []
    prev = -1
    for i in sorted(keep):
        if prev != -1 and i != prev + 1:
            out.append(f"[... {i - prev - 1} lines omitted ...]")
        elif prev == -1 and i > 0:
            out.append(f"[... {i} lines omitted ...]")
        out.append(lines[i])
        prev = i
    return truncate("\n".join(out), max_tokens, keep="tail")


@collector("failed_jobs")
def failed_jobs(ctx: CollectContext) -> list[dict[str, Any]]:
    pid = ctx.target["project_id"]
    jobs = [j for j in ctx.gl.pipeline_jobs(pid, ctx.target["pipeline_id"], scope="failed") if not j.get("allow_failure")]
    if not jobs:
        return []
    jobs = jobs[:5]
    per_job = max(ctx.budget_tokens // len(jobs), 400)
    out = []
    for job in jobs:
        out.append({
            "id": job["id"],
            "name": job.get("name"),
            "stage": job.get("stage"),
            "failure_reason": job.get("failure_reason"),
            "runner_tags": job.get("tag_list", []),
            "trace": focus_trace(ctx.gl.job_trace(pid, job["id"]), per_job),
        })
    return out


@collector("ci_config")
def ci_config(ctx: CollectContext) -> dict[str, Any]:
    pid = ctx.target["project_id"]
    raw = ctx.gl.raw_file(pid, ".gitlab-ci.yml", ctx.sha) or ""
    merged = ""
    try:
        lint = ctx.gl.ci_lint_ref(pid, ctx.sha)
        merged = lint.get("merged_yaml") or ""
    except Exception:  # noqa: BLE001 - linting needs extra permission on some instances; raw file is enough
        pass
    half = ctx.budget_tokens // 2
    return {
        "gitlab_ci_yml": truncate(raw, half, keep="head"),
        "merged_yaml": truncate(merged, half, keep="head") if merged and merged != raw else "",
    }


def _format_diffs(diffs: list[dict[str, Any]], max_tokens: int) -> str:
    parts = []
    for d in diffs:
        header = f"--- {d.get('old_path')}\n+++ {d.get('new_path')}"
        if d.get("new_file"):
            header += " (new file)"
        if d.get("deleted_file"):
            header += " (deleted)"
        parts.append(f"{header}\n{d.get('diff', '')}")
    return truncate("\n".join(parts), max_tokens, keep="head")


@collector("pipeline_diff")
def pipeline_diff(ctx: CollectContext) -> dict[str, Any]:
    """The change between the last green pipeline on this ref and the failing SHA."""
    pid = ctx.target["project_id"]
    green = ctx.gl.pipelines(pid, ref=ctx.ref, status="success", per_page=1, order_by="id", sort="desc")
    if green and green[0].get("sha") and green[0]["sha"] != ctx.sha:
        cmp = ctx.gl.compare(pid, green[0]["sha"], ctx.sha)
        commits = [f"{c['short_id']} {c['title']}" for c in cmp.get("commits", [])][-20:]
        return {
            "base": green[0]["sha"],
            "commits": commits,
            "diff": _format_diffs(cmp.get("diffs", []), ctx.budget_tokens - len("\n".join(commits)) // CHARS_PER_TOKEN),
        }
    return {"base": None, "commits": [], "diff": _format_diffs(ctx.gl.commit_diff(pid, ctx.sha), ctx.budget_tokens)}
