"""Task persistence and a small database-backed queue (Postgres in production, SQLite in tests)."""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Collection

from sqlalchemy import (DateTime, Index, Integer, String, Text, UniqueConstraint, create_engine, delete, false, func,
                        inspect, select, text, update)
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

QUEUED = "queued"
DISPATCHING = "dispatching"
DISPATCHED = "dispatched"
RESULT_RECEIVED = "result_received"
DONE = "done"
FAILED = "failed"
ACTIVE = (QUEUED, DISPATCHING, DISPATCHED, RESULT_RECEIVED)

# Flow run states
RUNNING = "running"
WAITING_APPROVAL = "waiting_approval"
STOPPED = "stopped"  # a step did not succeed, or someone said "@agent stop"
FLOW_ACTIVE = (RUNNING, WAITING_APPROVAL)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    agent: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    project_path: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True)
    subject_key: Mapped[str] = mapped_column(String(255), index=True)
    command: Mapped[str | None] = mapped_column(String(64), nullable=True)
    instruction: Mapped[str] = mapped_column(Text, default="")
    target_json: Mapped[str] = mapped_column(Text)
    bundle_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    sandbox_pipeline_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)  # link or short description of what was produced
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Columns added after the first release must stay nullable: _migrate() adds them to existing databases.
    tool_calls: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    agent_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # the definition the task runs with
    flow_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    flow_step: Mapped[int | None] = mapped_column(Integer, nullable=True)
    produced_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # what the output handler created
    tool_log_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # tool calls and results, for eval export

    @property
    def target(self) -> dict[str, Any]:
        return json.loads(self.target_json)

    @property
    def produced(self) -> dict[str, Any]:
        return json.loads(self.produced_json) if self.produced_json else {}

    @property
    def bundle(self) -> dict[str, Any] | None:
        return json.loads(self.bundle_json) if self.bundle_json else None

    @property
    def tool_log(self) -> list[dict[str, Any]]:
        return json.loads(self.tool_log_json) if self.tool_log_json else []

    @property
    def result(self) -> dict[str, Any] | None:
        return json.loads(self.result_json) if self.result_json else None


class FlowRun(Base):
    """One execution of a multi-agent flow on a subject (an issue or merge request)."""

    __tablename__ = "flow_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    flow: Mapped[str] = mapped_column(String(64))
    project_id: Mapped[int] = mapped_column(Integer)
    project_path: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True)
    subject_key: Mapped[str] = mapped_column(String(255), index=True)
    step: Mapped[int] = mapped_column(Integer, default=0)
    target_json: Mapped[str] = mapped_column(Text)
    instruction: Mapped[str] = mapped_column(Text, default="")
    status_note_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    @property
    def target(self) -> dict[str, Any]:
        return json.loads(self.target_json)


class TaskLink(Base):
    """A GitLab object (an MR or a note) that a task created, so reactions and outcomes can find the task."""

    __tablename__ = "task_links"
    __table_args__ = (Index("ix_task_links_ref", "project_id", "kind", "ref_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(32), index=True)
    agent: Mapped[str] = mapped_column(String(64))
    project_id: Mapped[int] = mapped_column(Integer)
    project_path: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(16))  # mr | note
    ref_id: Mapped[int] = mapped_column(Integer)  # MR iid, or note id
    noteable_type: Mapped[str | None] = mapped_column(String(32), nullable=True)  # for notes: issue | merge_request
    noteable_iid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# Feedback kinds. Reactions come from people; outcomes from what happened to the agent's work.
THUMBS_UP = "thumbs_up"
THUMBS_DOWN = "thumbs_down"
MR_MERGED = "mr_merged"
MR_CLOSED = "mr_closed"
PIPELINE_GREEN = "pipeline_green"
REACTIONS = (THUMBS_UP, THUMBS_DOWN)


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (UniqueConstraint("task_id", "kind", "actor", name="uq_feedback_once"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(32), index=True)
    agent: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(255), default="")  # "" for outcomes
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class TaskStore:
    def __init__(self, database_url: str):
        kwargs: dict[str, Any] = {"pool_pre_ping": True}
        if database_url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        self.engine = create_engine(database_url, **kwargs)
        self.is_sqlite = database_url.startswith("sqlite")
        # API and worker start together; tolerate the other process creating the schema first.
        for attempt in range(5):
            try:
                Base.metadata.create_all(self.engine)
                break
            except (OperationalError, ProgrammingError, IntegrityError):
                if attempt == 4:
                    raise
                time.sleep(2)
        self._migrate()
        self.Session = sessionmaker(self.engine, expire_on_commit=False)

    def _migrate(self) -> None:
        """Add columns that newer releases introduced. create_all() creates missing tables but never alters one."""
        for table in Base.metadata.sorted_tables:
            existing = {c["name"] for c in inspect(self.engine).get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                ddl = column.type.compile(dialect=self.engine.dialect)
                try:
                    with self.engine.begin() as conn:
                        conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {column.name} {ddl}'))
                except (OperationalError, ProgrammingError):
                    pass  # another process added it first

    def create(self, *, agent: str, project_id: int, project_path: str, dedupe_key: str, subject_key: str,
               target: dict[str, Any], command: str | None = None, instruction: str = "",
               agent_json: str | None = None, flow_run_id: str | None = None,
               flow_step: int | None = None) -> Task | None:
        """Insert a queued task. Returns None when an identical event was already handled."""
        task = Task(
            id=uuid.uuid4().hex,
            agent=agent,
            project_id=project_id,
            project_path=project_path,
            status=QUEUED,
            dedupe_key=dedupe_key,
            subject_key=subject_key,
            command=command,
            instruction=instruction,
            target_json=json.dumps(target),
            agent_json=agent_json,
            flow_run_id=flow_run_id,
            flow_step=flow_step,
        )
        with self.Session() as s:
            s.add(task)
            try:
                s.commit()
            except IntegrityError:
                s.rollback()
                return None
        return task

    def get(self, task_id: str) -> Task | None:
        with self.Session() as s:
            return s.get(Task, task_id)

    def count_for_subject(self, agent: str, subject_key: str, statuses: tuple[str, ...] | None = None) -> int:
        with self.Session() as s:
            q = select(func.count()).select_from(Task).where(Task.agent == agent, Task.subject_key == subject_key)
            if statuses:
                q = q.where(Task.status.in_(statuses))
            return s.scalar(q) or 0

    def claim_queued(self, capacity: int, lane: Collection[str] = (), in_lane: bool = False) -> list[Task]:
        """Atomically move up to `capacity` free slots' worth of queued tasks to DISPATCHING.

        `lane` names agents with their own capacity (inline chat must not wait behind sandbox jobs):
        in_lane=True claims only those agents, in_lane=False everything else.
        """
        def scoped(q):  # noqa: ANN001, ANN202
            if not lane:
                return q.where(false()) if in_lane else q
            return q.where(Task.agent.in_(lane) if in_lane else Task.agent.not_in(lane))

        with self.Session() as s, s.begin():
            in_flight = s.scalar(scoped(select(func.count()).select_from(Task)
                                        .where(Task.status.in_((DISPATCHING, DISPATCHED))))) or 0
            free = capacity - in_flight
            if free <= 0:
                return []
            q = scoped(select(Task).where(Task.status == QUEUED)).order_by(Task.created_at).limit(free)
            if not self.is_sqlite:
                q = q.with_for_update(skip_locked=True)
            tasks = list(s.scalars(q))
            for t in tasks:
                t.status = DISPATCHING
            return tasks

    def count_since(self, agent: str, project_id: int, since: datetime) -> int:
        with self.Session() as s:
            return s.scalar(select(func.count()).select_from(Task).where(
                Task.agent == agent, Task.project_id == project_id, Task.created_at >= since)) or 0

    def list_by_status(self, *statuses: str) -> list[Task]:
        with self.Session() as s:
            return list(s.scalars(select(Task).where(Task.status.in_(statuses)).order_by(Task.created_at)))

    def recent(self, limit: int = 100) -> list[Task]:
        with self.Session() as s:
            return list(s.scalars(select(Task).order_by(Task.created_at.desc()).limit(limit)))

    def update(self, task_id: str, **fields: Any) -> None:
        with self.Session() as s, s.begin():
            task = s.get(Task, task_id)
            if task is None:
                raise KeyError(task_id)
            for key, value in fields.items():
                if key in ("bundle", "result", "produced"):
                    setattr(task, f"{key}_json", json.dumps(value))
                else:
                    setattr(task, key, value)

    def use_tool_call(self, task_id: str, cap: int) -> bool:
        """Count one tool call against the task's budget. False when the task is not running or the cap is reached."""
        with self.Session() as s, s.begin():
            used = func.coalesce(Task.tool_calls, 0)
            res = s.execute(update(Task).where(Task.id == task_id, Task.status == DISPATCHED, used < cap)
                            .values(tool_calls=used + 1))
            return res.rowcount == 1

    def log_tool_call(self, task_id: str, name: str, args: dict[str, Any], result: str,
                      max_entries: int = 60, max_chars: int = 8000) -> None:
        """Keep what a tool returned so a task can later be replayed offline as an eval fixture."""
        with self.Session() as s, s.begin():
            task = s.get(Task, task_id)
            if task is None:
                return
            log = json.loads(task.tool_log_json) if task.tool_log_json else []
            if len(log) >= max_entries:
                return
            log.append({"name": name, "args": args, "result": result[:max_chars]})
            task.tool_log_json = json.dumps(log)

    def accept_result(self, task_id: str, result: dict[str, Any]) -> bool:
        """Store a sandbox result once. A second post (or a post after timeout) is rejected."""
        with self.Session() as s, s.begin():
            q = select(Task).where(Task.id == task_id)
            if not self.is_sqlite:
                q = q.with_for_update()
            task = s.scalars(q).first()
            if task is None or task.status != DISPATCHED:
                return False
            task.result_json = json.dumps(result)
            task.status = RESULT_RECEIVED
            return True

    # -- flow runs ------------------------------------------------------------------------
    def create_run(self, *, flow: str, project_id: int, project_path: str, dedupe_key: str, subject_key: str,
                   target: dict[str, Any], instruction: str = "") -> FlowRun | None:
        run = FlowRun(id=uuid.uuid4().hex, flow=flow, project_id=project_id, project_path=project_path, status=RUNNING,
                      dedupe_key=dedupe_key, subject_key=subject_key, step=0, target_json=json.dumps(target),
                      instruction=instruction)
        with self.Session() as s:
            s.add(run)
            try:
                s.commit()
            except IntegrityError:
                s.rollback()
                return None
        return run

    def get_run(self, run_id: str) -> FlowRun | None:
        with self.Session() as s:
            return s.get(FlowRun, run_id)

    def update_run(self, run_id: str, **fields: Any) -> None:
        with self.Session() as s, s.begin():
            run = s.get(FlowRun, run_id)
            if run is None:
                raise KeyError(run_id)
            for key, value in fields.items():
                setattr(run, key, value)

    def move_run(self, run_id: str, *, from_step: int, from_status: str, to_step: int, to_status: str) -> bool:
        """Compare-and-set a run's position. Only one caller wins when parallel tasks finish together."""
        with self.Session() as s, s.begin():
            res = s.execute(update(FlowRun).where(FlowRun.id == run_id, FlowRun.step == from_step,
                                                  FlowRun.status == from_status)
                            .values(step=to_step, status=to_status, updated_at=utcnow()))
            return res.rowcount == 1

    def active_run(self, subject_key: str) -> FlowRun | None:
        with self.Session() as s:
            q = select(FlowRun).where(FlowRun.subject_key == subject_key, FlowRun.status.in_(FLOW_ACTIVE))
            return s.scalars(q.order_by(FlowRun.created_at.desc())).first()

    def active_runs(self, project_path: str | None = None, limit: int = 50) -> list[FlowRun]:
        with self.Session() as s:
            q = select(FlowRun).where(FlowRun.status.in_(FLOW_ACTIVE))
            if project_path:
                q = q.where(FlowRun.project_path == project_path)
            return list(s.scalars(q.order_by(FlowRun.created_at.desc()).limit(limit)))

    def run_tasks(self, run_id: str, step: int | None = None) -> list[Task]:
        with self.Session() as s:
            q = select(Task).where(Task.flow_run_id == run_id)
            if step is not None:
                q = q.where(Task.flow_step == step)
            return list(s.scalars(q.order_by(Task.created_at)))

    # -- links & feedback -------------------------------------------------------------------
    def add_links(self, task: Task, links: list[dict[str, Any]]) -> None:
        if not links:
            return
        with self.Session() as s, s.begin():
            for link in links:
                s.add(TaskLink(task_id=task.id, agent=task.agent, project_id=task.project_id,
                               project_path=task.project_path, **link))

    def task_for(self, project_id: int, kind: str, ref_id: int) -> Task | None:
        """The task that created an MR (kind="mr", ref_id=iid) or a note (kind="note", ref_id=note id)."""
        with self.Session() as s:
            link = s.scalars(select(TaskLink).where(TaskLink.project_id == project_id, TaskLink.kind == kind,
                                                    TaskLink.ref_id == ref_id)
                             .order_by(TaskLink.created_at)).first()
            return s.get(Task, link.task_id) if link else None

    def tasks_with_prefix(self, project_id: int, prefix: str) -> list[Task]:
        with self.Session() as s:
            return list(s.scalars(select(Task).where(Task.project_id == project_id, Task.id.like(f"{prefix}%"))
                                  .order_by(Task.created_at.desc())))

    def links_since(self, since: datetime, limit: int = 500) -> list[TaskLink]:
        with self.Session() as s:
            return list(s.scalars(select(TaskLink).where(TaskLink.created_at >= since)
                                  .order_by(TaskLink.created_at.desc()).limit(limit)))

    def add_feedback(self, task: Task, kind: str, actor: str = "") -> bool:
        """Record one piece of feedback. False when it was already recorded."""
        with self.Session() as s:
            s.add(Feedback(task_id=task.id, agent=task.agent, project_id=task.project_id, kind=kind, actor=actor))
            try:
                s.commit()
                return True
            except IntegrityError:
                s.rollback()
                return False

    def sync_reactions(self, task: Task, current: set[tuple[str, str]]) -> None:
        """Make the stored reactions of a task match `current` {(kind, actor)}: reactions can be withdrawn."""
        with self.Session() as s, s.begin():
            rows = list(s.scalars(select(Feedback).where(Feedback.task_id == task.id, Feedback.kind.in_(REACTIONS))))
            stored = {(r.kind, r.actor) for r in rows}
            gone = [r.id for r in rows if (r.kind, r.actor) not in current]
            if gone:
                s.execute(delete(Feedback).where(Feedback.id.in_(gone)))
            for kind, actor in current - stored:
                s.add(Feedback(task_id=task.id, agent=task.agent, project_id=task.project_id, kind=kind, actor=actor))

    def feedback_for(self, task_ids: Collection[str]) -> list[Feedback]:
        if not task_ids:
            return []
        with self.Session() as s:
            return list(s.scalars(select(Feedback).where(Feedback.task_id.in_(list(task_ids)))))

    def tasks_since(self, since: datetime, project_path: str | None = None, limit: int = 20000) -> list[Task]:
        with self.Session() as s:
            q = select(Task).where(Task.created_at >= since)
            if project_path:
                q = q.where(Task.project_path == project_path)
            return list(s.scalars(q.order_by(Task.created_at.desc()).limit(limit)))

    def project_paths(self) -> list[str]:
        with self.Session() as s:
            return sorted(s.scalars(select(Task.project_path).distinct()))

    @staticmethod
    def deadline_for(minutes: int) -> datetime:
        return utcnow() + timedelta(minutes=minutes)
