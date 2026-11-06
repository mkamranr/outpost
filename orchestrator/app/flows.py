"""Multi-agent flows: run agents in sequence (or in parallel within a step), with human approval gates.

A flow run lives on one subject (an issue or MR). Each step creates ordinary tasks tagged with the run; when the
last task of a step finishes, the run moves on. Tasks are created here directly, never through webhooks, so the
router's rule of ignoring bot-authored events still holds and agents cannot trigger each other in loops.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .events import Event
from .gitlab_client import DEVELOPER
from .outputs.common import sanitize
from .registry import APPROVAL_LABEL, FlowDef, FlowStep
from .router import FlowMatch, parse_mention
from .tasks import ACTIVE, DONE, RUNNING, STOPPED, WAITING_APPROVAL, FlowRun, Task

if TYPE_CHECKING:
    from .platform import Platform

log = logging.getLogger("orchestrator.flows")

STATUS_MARKER = "<!-- agent:flow -->"
FINISHED = "done"


def control(event: Event, mention: str) -> tuple[str, str] | None:
    """("approve" | "stop", instruction) when the event is a human controlling a flow, else None."""
    if event.kind == "note":
        parsed = parse_mention(event.note_body or "", mention)
        if parsed and parsed[0] in ("approve", "stop"):
            return parsed
    elif event.kind in ("issue", "merge_request") and APPROVAL_LABEL in event.added_labels:
        return "approve", ""
    return None


def subject_key(event: Event) -> str:
    return f"{event.project_id}:{event.noteable_type}:{event.iid or event.sha}"


def task_ok(task: Task) -> bool:
    return task.status == DONE and bool(task.produced.get("ok"))


def mr_target(run: FlowRun, iid: int) -> dict[str, Any]:
    base = run.target
    return {"kind": "merge_request", "project_id": run.project_id, "project_path": run.project_path,
            "actor_id": base.get("actor_id"), "actor_username": base.get("actor_username"),
            "noteable_type": "merge_request", "iid": iid, "note_id": None, "note_body": None, "discussion_id": None,
            "note_position": None, "pipeline_id": None, "ref": None, "sha": None, "failed_job_ids": []}


def _icon(task: Task) -> str:
    if task.status in ACTIVE:
        return "⏳"
    return "✅" if task_ok(task) else "❌"


def render_status(run: FlowRun, flow: FlowDef, tasks: list[Task], mention: str) -> str:
    """The checklist note kept up to date on the subject while the flow runs."""
    state = {RUNNING: "running", WAITING_APPROVAL: "waiting for approval", FINISHED: "finished",
             STOPPED: "stopped"}.get(run.status, run.status)
    rows = [STATUS_MARKER, f"**Flow `{flow.name}`**: {state}", "", "| # | Step | Status |", "| --- | --- | --- |"]
    by_step: dict[int, list[Task]] = {}
    for t in tasks:
        by_step.setdefault(t.flow_step or 0, []).append(t)
    for i, step in enumerate(flow.steps):
        if step.gate:
            if i < run.step or (i == run.step and run.status != WAITING_APPROVAL):
                status = "✅ approved"
            elif i == run.step:
                status = f"⏸️ reply `{mention} approve` (optionally with corrections) or add label `{APPROVAL_LABEL}`"
            else:
                status = "⬜ pending"
        elif by_step.get(i):
            status = "<br>".join(f"{_icon(t)} `{t.agent}`: {sanitize(t.outcome or t.error or t.status, 200)}"
                                 for t in by_step[i])
        else:
            status = "⬜ pending" if run.status in (RUNNING, WAITING_APPROVAL) else "➖ skipped"
        rows.append(f"| {i + 1} | {step.label()} | {status.replace('|', '/').replace(chr(10), ' ')} |")
    if run.outcome:
        rows += ["", sanitize(run.outcome, 500)]
    if run.status in (RUNNING, WAITING_APPROVAL):
        rows += ["", f"<sub>`{mention} stop` stops this flow.</sub>"]
    return "\n".join(rows)


class FlowEngine:
    def __init__(self, platform: "Platform"):
        self.p = platform

    @property
    def store(self):  # noqa: ANN201
        return self.p.store

    # -- starting and controlling ---------------------------------------------------------
    def start(self, match: FlowMatch, event: Event) -> dict[str, Any]:
        active = self.store.active_run(match.subject_key)
        cfg = self.p.projects.for_project(event.project_path)
        gl = self.p.project_gl(cfg)
        if active:
            self.p._reply(gl, event, f"Flow `{active.flow}` is already running here. "
                                     f"Reply `{self.p.settings.mention} stop` to stop it first.")
            return {"flow": match.flow.name, "skipped": "flow already running"}
        run = self.store.create_run(flow=match.flow.name, project_id=event.project_id, project_path=event.project_path,
                                    dedupe_key=match.dedupe_key, subject_key=match.subject_key, target=event.target(),
                                    instruction=match.instruction)
        if run is None:
            return {"flow": match.flow.name, "skipped": "duplicate event"}
        self._enter(run.id, 0, [])
        return {"flow": match.flow.name, "run": run.id}

    def handle_control(self, event: Event, action: str, instruction: str) -> dict[str, Any] | None:
        """Approve or stop the active run on the event's subject. None when there is no run to control."""
        run = self.store.active_run(subject_key(event))
        if run is None:
            return None
        cfg = self.p.projects.for_project(event.project_path)
        gl = self.p.project_gl(cfg)
        if not event.actor_id or gl.member_access_level(event.project_id, event.actor_id) < DEVELOPER:
            return {"ignored": "actor lacks Developer access"}
        if action == "stop":
            self.store.update_run(run.id, status=STOPPED, outcome=f"Stopped by @{event.actor_username}.")
            self.refresh(run.id)
            return {"flow": run.flow, "stopped": run.id}
        if run.status != WAITING_APPROVAL:
            if event.kind != "note":
                return {"ignored": "flow is not waiting for approval"}
            self.p._reply(gl, event, f"Flow `{run.flow}` is not waiting for approval right now.")
            return {"flow": run.flow, "replied": "not waiting"}
        if not self.store.move_run(run.id, from_step=run.step, from_status=WAITING_APPROVAL,
                                   to_step=run.step + 1, to_status=RUNNING):
            return {"ignored": "already approved"}
        if instruction:
            self.store.update_run(run.id, instruction=instruction)
        self._enter(run.id, run.step + 1, [], moved=True)
        return {"flow": run.flow, "approved": run.id}

    # -- progress ----------------------------------------------------------------------------
    def on_task_finished(self, task: Task) -> None:
        if not task.flow_run_id:
            return
        run = self.store.get_run(task.flow_run_id)
        if run is None or run.status != RUNNING or task.flow_step != run.step:
            if run is not None:
                self.refresh(run.id)
            return
        tasks = self.store.run_tasks(run.id, run.step)
        if any(t.status in ACTIVE for t in tasks):
            self.refresh(run.id)  # a parallel sibling is still working
            return
        if not self.store.move_run(run.id, from_step=run.step, from_status=RUNNING, to_step=run.step + 1,
                                   to_status=RUNNING):
            return  # another worker advanced it
        self._enter(run.id, run.step + 1, tasks, moved=True)

    def _enter(self, run_id: str, index: int, previous: list[Task], moved: bool = False) -> None:
        """Start step `index` (the run's step is already `index` when `moved`)."""
        run = self.store.get_run(run_id)
        flow = self.p.registry.flows.get(run.flow)
        if flow is None:
            self._finish(run, STOPPED, "This flow no longer exists on the platform.")
            return
        if not moved and run.step != index:
            self.store.update_run(run.id, step=index)
        if index >= len(flow.steps):
            self._finish(run, FINISHED, "")
            return
        step = flow.steps[index]
        if step.when == "success" and previous and not all(task_ok(t) for t in previous):
            failed = ", ".join(f"`{t.agent}`" for t in previous if not task_ok(t))
            self._finish(run, STOPPED, f"Stopped because {failed} did not succeed.")
            return
        if step.gate:
            self.store.update_run(run.id, status=WAITING_APPROVAL)
            self.refresh(run.id)
            return
        target = self._target(run, step, previous)
        if target is None:
            self._finish(run, STOPPED, "Stopped because the previous step did not produce a merge request.")
            return
        cfg = self.p.projects.for_project(run.project_path)
        if cfg is None:
            self._finish(run, STOPPED, "The project is no longer onboarded.")
            return
        disabled = [name for name in step.agents if name not in cfg.agents]
        if disabled:
            self._finish(run, STOPPED, f"Stopped because `{disabled[0]}` is not enabled for this project.")
            return
        subject = f"{run.project_id}:{target['noteable_type']}:{target.get('iid') or target.get('sha')}"
        for name in step.agents:
            agent = self.p.registry.get(name)
            self.store.create(agent=name, project_id=run.project_id, project_path=run.project_path,
                              dedupe_key=f"flow:{run.id}:{index}:{name}", subject_key=subject, target=target,
                              command=f"flow:{flow.name}", instruction=run.instruction,
                              agent_json=agent.model_dump_json(), flow_run_id=run.id, flow_step=index)
        self.refresh(run.id)

    def _target(self, run: FlowRun, step: FlowStep, previous: list[Task]) -> dict[str, Any] | None:
        if step.target == "subject":
            return run.target
        # produced_mr: the MR made by the most recent agent step (a gate step has no tasks, so look back).
        tasks = previous or [t for t in self.store.run_tasks(run.id) if (t.flow_step or 0) < run.step]
        for task in reversed(tasks):
            iid = task.produced.get("mr_iid")
            if iid:
                return mr_target(run, int(iid))
        return None

    def _finish(self, run: FlowRun, status: str, outcome: str) -> None:
        self.store.update_run(run.id, status=status, outcome=outcome or None)
        self.refresh(run.id)

    # -- status note ----------------------------------------------------------------------------
    def refresh(self, run_id: str) -> None:
        """Create or update the checklist note on the subject. Cosmetic: failures are logged, never raised."""
        run = self.store.get_run(run_id)
        flow = self.p.registry.flows.get(run.flow) if run else None
        t = run.target if run else {}
        if run is None or flow is None or t.get("noteable_type") not in ("issue", "merge_request") or not t.get("iid"):
            return
        try:
            cfg = self.p.projects.for_project(run.project_path) or self.p.projects.defaults
            gl = self.p.project_gl(cfg)
            body = render_status(run, flow, self.store.run_tasks(run.id), self.p.settings.mention)
            if run.status_note_id:
                gl.update_note(run.project_id, t["noteable_type"], t["iid"], run.status_note_id, body)
            else:
                note = gl.create_note(run.project_id, t["noteable_type"], t["iid"], body)
                if isinstance(note, dict) and note.get("id"):
                    self.store.update_run(run.id, status_note_id=int(note["id"]))
        except Exception:  # noqa: BLE001
            log.exception("could not update the status note of flow run %s", run_id)

    def context_for(self, task: Task) -> str:
        """What earlier steps of the task's flow produced, for the prompt of a later step."""
        if not task.flow_run_id:
            return ""
        run = self.store.get_run(task.flow_run_id)
        if run is None:
            return ""
        earlier = [t for t in self.store.run_tasks(run.id) if (t.flow_step or 0) < (task.flow_step or 0)]
        if not earlier:
            return ""
        lines = []
        for t in earlier:
            summary = t.produced.get("summary") or ""
            lines.append(f"- step {(t.flow_step or 0) + 1} `{t.agent}`: {t.outcome or t.status}"
                         + (f"\n  {summary[:1500]}" if summary else ""))
        return (f"\n\n## Earlier steps of the `{run.flow}` flow\n"
                "<untrusted-data source=\"earlier agent output\">\n" + "\n".join(lines) + "\n</untrusted-data>\n")
