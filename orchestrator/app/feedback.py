"""Was the agent's work any good? Links GitLab objects back to tasks and records reactions and outcomes.

Signals, per task:
- 👍 / 👎 award emoji on the notes and MRs the task posted (polled; reactions can be withdrawn)
- the agent's MR was merged, or closed without merging
- a pipeline on the agent's branch went green after the agent's commit
"""
from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from .events import Event
from .tasks import MR_CLOSED, MR_MERGED, PIPELINE_GREEN, THUMBS_DOWN, THUMBS_UP, Task, utcnow

if TYPE_CHECKING:
    from .outputs.common import OutputContext
    from .platform import Platform

log = logging.getLogger("orchestrator.feedback")

EMOJI = {"thumbsup": THUMBS_UP, "+1": THUMBS_UP, "thumbsdown": THUMBS_DOWN, "-1": THUMBS_DOWN}
_COMMIT_TASK = re.compile(r"\(task ([0-9a-f]{32})\)")
_BRANCH_TASK = re.compile(r"^agent/.+-([0-9a-f]{6})$")


def links_for(ctx: "OutputContext") -> list[dict[str, Any]]:
    """What a finished task created that people may react to: the MR it opened and the notes it posted."""
    t = ctx.target
    links: list[dict[str, Any]] = []
    mr_iid = ctx.produced.get("mr_iid")
    # A commit pushed onto an existing MR does not make that MR the task's work (it may be a human's MR).
    own_mr = not (t.get("noteable_type") == "merge_request" and mr_iid == t.get("iid"))
    if isinstance(mr_iid, int) and own_mr:
        links.append({"kind": "mr", "ref_id": mr_iid})
    if t.get("noteable_type") in ("issue", "merge_request") and t.get("iid"):
        note_ids = list(ctx.notes)
        if isinstance(ctx.produced.get("note_id"), int):
            note_ids.append(ctx.produced["note_id"])
        for note_id in dict.fromkeys(note_ids):
            links.append({"kind": "note", "ref_id": note_id, "noteable_type": t["noteable_type"],
                          "noteable_iid": t["iid"]})
    return links


def _task_for_branch(platform: "Platform", project_id: int, branch: str | None) -> Task | None:
    m = _BRANCH_TASK.match(branch or "")
    if not m:
        return None
    for task in platform.store.tasks_with_prefix(project_id, m.group(1)):
        if branch.startswith(f"agent/{task.agent}-"):
            return task
    return None


def record_outcome(platform: "Platform", event: Event) -> str | None:
    """Record what happened to an agent's MR or branch. Returns the kind recorded, for logs and tests."""
    store = platform.store
    if event.kind == "merge_request" and event.action in ("merge", "close") and event.iid:
        task = store.task_for(event.project_id, "mr", event.iid) or _task_for_branch(platform, event.project_id, event.ref)
        kind = MR_MERGED if event.action == "merge" else MR_CLOSED
    elif event.kind == "pipeline" and event.pipeline_status == "success" and (event.ref or "").startswith("agent/"):
        m = _COMMIT_TASK.search(event.commit_message or "")
        task = store.get(m.group(1)) if m else None
        if task is None or task.project_id != event.project_id:
            task = _task_for_branch(platform, event.project_id, event.ref)
        kind = PIPELINE_GREEN
    else:
        return None
    if task is None:
        return None
    return kind if store.add_feedback(task, kind) else None


class ReactionPoller:
    """Reads award emoji on recent agent notes and MRs. Webhooks do not report emoji, so this polls."""

    def __init__(self, platform: "Platform"):
        self.platform = platform
        self._last = time.monotonic()  # first poll one interval after start

    def maybe_poll(self) -> None:
        interval = self.platform.settings.feedback_poll_seconds
        if interval <= 0 or time.monotonic() - self._last < interval:
            return
        self._last = time.monotonic()
        try:
            self.poll()
        except Exception:  # noqa: BLE001 - feedback is best effort
            log.exception("polling reactions failed")

    def poll(self) -> int:
        """Sync reactions for every task with links in the window. Returns the number of tasks synced."""
        p = self.platform
        since = utcnow() - timedelta(days=p.settings.feedback_window_days)
        by_task: dict[str, list[Any]] = defaultdict(list)
        for link in p.store.links_since(since):
            by_task[link.task_id].append(link)
        synced = 0
        for task_id, links in by_task.items():
            task = p.store.get(task_id)
            if task is None:
                continue
            cfg = p.projects.for_project(task.project_path)
            if cfg is None:
                continue
            gl = p.project_gl(cfg)
            current: set[tuple[str, str]] = set()
            complete = True
            for link in links:
                try:
                    if link.kind == "mr":
                        awards = gl.mr_award_emoji(link.project_id, link.ref_id)
                    else:
                        awards = gl.note_award_emoji(link.project_id, link.noteable_type, link.noteable_iid, link.ref_id)
                except Exception:  # noqa: BLE001 - deleted notes, lost access: keep what we had
                    complete = False
                    continue
                for award in awards or []:
                    kind = EMOJI.get(str(award.get("name")))
                    actor = str((award.get("user") or {}).get("username") or "")
                    if kind and actor and actor != p.settings.bot_username:
                        current.add((kind, actor))
            if complete:
                p.store.sync_reactions(task, current)
            else:
                for kind, actor in current:
                    p.store.add_feedback(task, kind, actor)
            synced += 1
        return synced
