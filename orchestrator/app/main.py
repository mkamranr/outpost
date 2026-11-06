"""HTTP API: GitLab webhooks in, sandbox bundle/result exchange, health and an operator task list."""
from __future__ import annotations

import hmac
import logging
from typing import Any, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from . import events, tickets
from .platform import Platform

log = logging.getLogger("orchestrator.api")
MAX_RESULT_BYTES = 25 * 1024 * 1024


class Change(BaseModel):
    action: Literal["create", "update", "delete", "move"]
    path: str
    previous_path: str | None = None
    content_b64: str | None = None


class Attempt(BaseModel):
    n: int
    tests_passed: bool | None = None
    test_output_tail: str = ""
    note: str = ""


class ReviewComment(BaseModel):
    path: str
    line: int
    body: str
    old_path: str | None = None


class SandboxResult(BaseModel):
    status: Literal["success", "no_change", "failed"]
    classification: str | None = None
    title: str | None = None
    summary: str = ""
    body: str | None = None
    tests_passed: bool | None = None
    attempts: list[Attempt] = Field(default_factory=list)
    changes: list[Change] = Field(default_factory=list)
    comments: list[ReviewComment] = Field(default_factory=list)
    diff: str = ""
    error: str | None = None


def create_app(platform: Platform) -> FastAPI:
    app = FastAPI(title="Agent Platform Orchestrator", docs_url=None, redoc_url=None)
    settings = platform.settings

    def ticket_for(task_id: str, authorization: str = Header(default="")) -> str:
        token = authorization.removeprefix("Bearer ").strip()
        if not tickets.verify(settings.ticket_secret, token, task_id):
            raise HTTPException(status_code=401, detail="invalid or expired ticket")
        return task_id

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "agents": sorted(platform.registry.agents)}

    @app.post("/webhook", status_code=202)
    async def webhook(request: Request, background: BackgroundTasks,
                      x_gitlab_token: str = Header(default=""), x_gitlab_event: str = Header(default="")) -> dict[str, Any]:
        if not hmac.compare_digest(x_gitlab_token, settings.webhook_secret):
            raise HTTPException(status_code=401, detail="bad webhook token")
        payload = await request.json()
        event = events.parse(x_gitlab_event, payload)
        if event is None:
            return {"ignored": "unrecognised payload"}

        def handle() -> None:
            try:
                log.info("event %s on %s: %s", event.kind, event.project_path, platform.handle_event(event))
            except Exception:  # noqa: BLE001
                log.exception("failed to handle %s event", event.kind)

        background.add_task(handle)
        return {"accepted": event.kind}

    @app.get("/api/tasks/{task_id}/bundle")
    def bundle(task_id: str = Depends(ticket_for)) -> dict[str, Any]:
        task = platform.store.get(task_id)
        if task is None or task.status != "dispatched" or task.bundle is None:
            raise HTTPException(status_code=409, detail="task is not awaiting a sandbox")
        return task.bundle

    @app.post("/api/tasks/{task_id}/result")
    async def result(request: Request, task_id: str = Depends(ticket_for)) -> dict[str, Any]:
        raw = await request.body()
        if len(raw) > MAX_RESULT_BYTES:
            raise HTTPException(status_code=413, detail="result too large")
        parsed = SandboxResult.model_validate_json(raw)
        if not platform.store.accept_result(task_id, parsed.model_dump()):
            raise HTTPException(status_code=409, detail="result already received or task not active")
        return {"accepted": True}

    @app.post("/api/tasks/{task_id}/tools/{name}")
    async def call_tool(name: str, request: Request, task_id: str = Depends(ticket_for)) -> dict[str, Any]:
        task = platform.store.get(task_id)
        if task is None or task.status != "dispatched":
            raise HTTPException(status_code=409, detail="task is not awaiting a sandbox")
        try:
            args = await request.json()
        except ValueError:
            raise HTTPException(status_code=400, detail="arguments must be JSON") from None
        try:
            # Tools make blocking GitLab calls; keep them off the event loop.
            content = await run_in_threadpool(platform.call_tool, task, name, args if isinstance(args, dict) else {})
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from None
        return {"content": content}

    @app.get("/api/tasks")
    def list_tasks(x_admin_token: str = Header(default=""), limit: int = 100) -> list[dict[str, Any]]:
        if not settings.admin_token or not hmac.compare_digest(x_admin_token, settings.admin_token):
            raise HTTPException(status_code=401, detail="admin token required")
        return [
            {
                "id": t.id, "agent": t.agent, "project": t.project_path, "status": t.status,
                "command": t.command, "outcome": t.outcome, "error": t.error,
                "sandbox_pipeline_id": t.sandbox_pipeline_id,
                "created_at": t.created_at.isoformat(), "updated_at": t.updated_at.isoformat(),
            }
            for t in platform.store.recent(min(limit, 500))
        ]

    return app


def build_platform() -> Platform:
    from .config import ProjectsConfig, Settings
    from .registry import Registry
    from .tasks import TaskStore

    settings = Settings.from_env()
    return Platform(settings, Registry.load(settings.agents_dir), ProjectsConfig.load(settings.projects_file),
                    TaskStore(settings.database_url))


def app_factory() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return create_app(build_platform())
