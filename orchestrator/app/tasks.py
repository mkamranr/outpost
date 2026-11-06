"""Task persistence and a small database-backed queue (Postgres in production, SQLite in tests)."""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Collection

from sqlalchemy import DateTime, Integer, String, Text, create_engine, false, func, inspect, select, text, update
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

    def run_tasks(self, run_id: str, step: int | None = None) -> list[Task]:
        with self.Session() as s:
            q = select(Task).where(Task.flow_run_id == run_id)
            if step is not None:
                q = q.where(Task.flow_step == step)
            return list(s.scalars(q.order_by(Task.created_at)))

    @staticmethod
    def deadline_for(minutes: int) -> datetime:
        return utcnow() + timedelta(minutes=minutes)
