"""Helpers shared by output handlers: safe text, where to comment, MR descriptions."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import ProjectConfig, Settings
from ..gitlab_client import GitLab
from ..registry import AgentDef
from ..tasks import Task

_QUICK_ACTION = re.compile(r"^(\s*)/(?=\w)", re.MULTILINE)
_MASS_MENTION = re.compile(r"@(all|here|channel)\b", re.IGNORECASE)


def sanitize(text: str | None, max_chars: int = 30000) -> str:
    """Model output is untrusted: stop it from running GitLab quick actions (/merge, /approve...) or pinging @all."""
    text = (text or "")[:max_chars]
    text = _QUICK_ACTION.sub(r"\1&#47;", text)
    return _MASS_MENTION.sub(lambda m: "@​" + m.group(1), text)


@dataclass
class OutputContext:
    gl: GitLab
    task: Task
    agent: AgentDef
    result: dict[str, Any]
    project: ProjectConfig
    settings: Settings
    # What the handler created, for flows: {"ok": bool, "mr_iid": int, "note_id": int, "summary": str}
    produced: dict[str, Any] = field(default_factory=dict)
    # Chat handoffs: (target, instruction) -> a markdown line saying what happened. Supplied by the platform.
    handoff: Callable[[str, str], str] | None = None

    @property
    def target(self) -> dict[str, Any]:
        return self.task.target

    @property
    def bundle(self) -> dict[str, Any]:
        return self.task.bundle or {}

    def footer(self) -> str:
        return (f"\n\n---\n<sub>🤖 `{self.agent.name}` agent · task `{self.task.id[:8]}` · "
                "AI-generated: review before relying on it.</sub>")

    def notify(self, body: str) -> Any:
        """Comment where the task came from: the triggering thread, the issue/MR, or the commit."""
        t = self.target
        text = sanitize(body) + self.footer()
        pid = t["project_id"]
        if t.get("discussion_id") and t.get("noteable_type") in ("issue", "merge_request"):
            return self.gl.reply_to_discussion(pid, t["noteable_type"], t["iid"], t["discussion_id"], text)
        if t.get("noteable_type") in ("issue", "merge_request") and t.get("iid"):
            return self.gl.create_note(pid, t["noteable_type"], t["iid"], text)
        if t.get("sha"):
            return self.gl.create_commit_comment(pid, t["sha"], text)
        return None

    def attempts_markdown(self) -> str:
        attempts = self.result.get("attempts") or []
        if not attempts:
            return ""
        rows = ["| Attempt | Tests | Notes |", "| --- | --- | --- |"]
        for a in attempts:
            status = "✅ pass" if a.get("tests_passed") else ("➖ not run" if a.get("tests_passed") is None else "❌ fail")
            rows.append(f"| {a.get('n')} | {status} | {sanitize(str(a.get('note', '')), 300).replace('|', '/')} |")
        last_tail = attempts[-1].get("test_output_tail")
        details = ""
        if last_tail:
            details = f"\n\n<details><summary>Last test output</summary>\n\n```\n{last_tail[-4000:]}\n```\n</details>"
        return "\n".join(rows) + details

    def branch_name(self, suffix: str) -> str:
        return f"agent/{self.agent.name}-{suffix}-{self.task.id[:6]}"


def failure_message(ctx: OutputContext, headline: str) -> str:
    parts = [f"**{headline}**"]
    if ctx.result.get("summary"):
        parts.append(ctx.result["summary"])
    if ctx.result.get("error"):
        parts.append(f"Error: `{str(ctx.result['error'])[:500]}`")
    attempts = ctx.attempts_markdown()
    if attempts:
        parts.append("What was tried:\n\n" + attempts)
    return "\n\n".join(parts)
