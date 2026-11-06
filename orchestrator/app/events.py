"""Normalise GitLab webhook payloads into one Event shape."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_NOTEABLE = {"Issue": "issue", "MergeRequest": "merge_request", "Commit": "commit"}


@dataclass
class Event:
    kind: str  # pipeline | note | issue | merge_request | other
    project_id: int
    project_path: str
    actor_id: int | None = None
    actor_username: str | None = None
    noteable_type: str | None = None  # issue | merge_request | commit
    iid: int | None = None  # issue or MR iid the event concerns
    note_id: int | None = None
    note_body: str | None = None
    discussion_id: str | None = None
    note_position: dict[str, Any] | None = None
    pipeline_id: int | None = None
    pipeline_status: str | None = None
    pipeline_source: str | None = None
    ref: str | None = None
    sha: str | None = None
    action: str | None = None
    labels: list[str] = field(default_factory=list)
    added_labels: list[str] = field(default_factory=list)
    failed_job_ids: list[int] = field(default_factory=list)
    has_new_commits: bool = False  # MR "update" events that carry a push (oldrev set)

    def target(self) -> dict[str, Any]:
        """The serialisable subset stored on the task and used by collectors and outputs."""
        return {
            "kind": self.kind,
            "project_id": self.project_id,
            "project_path": self.project_path,
            "actor_id": self.actor_id,
            "actor_username": self.actor_username,
            "noteable_type": self.noteable_type,
            "iid": self.iid,
            "note_id": self.note_id,
            "note_body": self.note_body,
            "discussion_id": self.discussion_id,
            "note_position": self.note_position,
            "pipeline_id": self.pipeline_id,
            "ref": self.ref,
            "sha": self.sha,
            "failed_job_ids": self.failed_job_ids,
        }


def _label_titles(labels: Any) -> list[str]:
    return [l.get("title", "") for l in labels or [] if isinstance(l, dict)]


def parse(event_header: str, payload: dict[str, Any]) -> Event | None:
    project = payload.get("project") or {}
    if "id" not in project:
        return None
    user = payload.get("user") or {}
    base = dict(
        project_id=int(project["id"]),
        project_path=project.get("path_with_namespace", ""),
        actor_id=user.get("id"),
        actor_username=user.get("username"),
    )
    kind = payload.get("object_kind")
    attrs = payload.get("object_attributes") or {}

    if kind == "pipeline":
        mr = payload.get("merge_request") or {}
        failed = [
            int(b["id"])
            for b in payload.get("builds") or []
            if b.get("status") == "failed" and not b.get("allow_failure")
        ]
        return Event(
            kind="pipeline",
            pipeline_id=attrs.get("id"),
            pipeline_status=attrs.get("status"),
            pipeline_source=attrs.get("source"),
            ref=attrs.get("ref"),
            sha=attrs.get("sha"),
            iid=mr.get("iid"),
            noteable_type="merge_request" if mr.get("iid") else "commit",
            failed_job_ids=failed,
            **base,
        )

    if kind == "note":
        noteable = _NOTEABLE.get(attrs.get("noteable_type", ""))
        if noteable == "issue":
            iid = (payload.get("issue") or {}).get("iid")
        elif noteable == "merge_request":
            iid = (payload.get("merge_request") or {}).get("iid")
        else:
            iid = None
        mr = payload.get("merge_request") or {}
        return Event(
            kind="note",
            noteable_type=noteable,
            iid=iid,
            note_id=attrs.get("id"),
            note_body=attrs.get("note") or "",
            discussion_id=attrs.get("discussion_id"),
            note_position=attrs.get("position"),
            sha=(payload.get("commit") or {}).get("id") or (mr.get("last_commit") or {}).get("id"),
            ref=mr.get("source_branch"),
            **base,
        )

    if kind in ("issue", "merge_request"):
        labels = _label_titles(payload.get("labels") or attrs.get("labels"))
        added: list[str] = []
        label_change = (payload.get("changes") or {}).get("labels")
        if label_change:
            before = set(_label_titles(label_change.get("previous")))
            after = _label_titles(label_change.get("current"))
            added = [l for l in after if l not in before]
        return Event(
            kind=kind,
            noteable_type=kind,
            iid=attrs.get("iid"),
            action=attrs.get("action"),
            labels=labels,
            added_labels=added,
            ref=attrs.get("source_branch"),
            sha=(attrs.get("last_commit") or {}).get("id"),
            has_new_commits=bool(attrs.get("oldrev")),
            **base,
        )

    return Event(kind="other", **base)
