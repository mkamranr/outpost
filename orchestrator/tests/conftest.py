from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
try:  # the orchestrator image installs agent-runner; in a dev checkout use the sibling package directly
    import agent_runner  # noqa: F401
except ImportError:
    sys.path.insert(0, str(ROOT / "sandbox"))

from app.config import ProjectsConfig, Settings
from app.gitlab_client import GitLab
from app.platform import Platform
from app.registry import Registry
from app.tasks import TaskStore

FIXTURES = Path(__file__).parent / "fixtures"


class FakeGitLab:
    """Route table over httpx.MockTransport. Records every call for assertions."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], Callable[[httpx.Request, re.Match[str]], Any]]] = []
        self.calls: list[tuple[str, str, Any]] = []

    def on(self, method: str, pattern: str, response: Any, prefix: str = "/api/v4") -> None:
        """Answer `method` on paths matching `pattern` (under /api/v4; pass prefix="/api" for /api/graphql)."""
        fn = response if callable(response) else (lambda req, m, r=response: r)
        self.routes.insert(0, (method, re.compile(f"^{prefix}{pattern}$"), fn))

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        self.calls.append((request.method, path, body))
        for method, pattern, fn in self.routes:
            m = pattern.match(path)
            if method == request.method and m:
                result = fn(request, m)
                if isinstance(result, httpx.Response):
                    return result
                if isinstance(result, str):
                    return httpx.Response(200, text=result)
                return httpx.Response(200, json=result)
        return httpx.Response(404, json={"message": f"no fake route for {request.method} {path}"})

    def client(self) -> GitLab:
        return GitLab("https://gitlab.example", "token", transport=httpx.MockTransport(self.handler))

    def posted(self, pattern: str) -> list[Any]:
        rx = re.compile(pattern)
        return [body for method, path, body in self.calls if method in ("POST", "PUT") and rx.search(path)]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        gitlab_url="https://gitlab.example",
        bot_username="agent-bot",
        webhook_secret="hook-secret",
        ticket_secret="ticket-secret",
        database_url=f"sqlite:///{tmp_path / 'tasks.db'}",
        public_url="https://orchestrator.example",
        sandbox_project="agent-platform/sandbox",
        agents_dir=str(ROOT / "agents"),
        admin_token="admin",
        chat_workers=0,  # run inline agents synchronously so tests are deterministic
    )


@pytest.fixture
def registry() -> Registry:
    return Registry.load(ROOT / "agents")


@pytest.fixture
def projects() -> ProjectsConfig:
    return ProjectsConfig(
        defaults={"sandbox_image": "registry.internal/agent/sandbox-python:1", "test": ["pytest -q"]},
        allowed_image_prefixes=["registry.internal/"],
        projects={"group/app": {"agents": ["pipeline-fixer", "planner", "code-writer", "code-reviewer", "mr-assist"]}},
    )


@pytest.fixture
def fake() -> FakeGitLab:
    return FakeGitLab()


@pytest.fixture
def platform(settings: Settings, registry: Registry, projects: ProjectsConfig, fake: FakeGitLab) -> Platform:
    client = fake.client()
    return Platform(settings, registry, projects, TaskStore(settings.database_url), gitlab_factory=lambda ref: client)


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))
