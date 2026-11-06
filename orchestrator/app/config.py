"""Orchestrator settings (environment) and per-project configuration (projects.yaml + .gitlab/agent.yml)."""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

# Agent configuration lives here; an agent must never be able to change what agents may do.
ALWAYS_PROTECTED = [".gitlab/agent.yml", ".gitlab/agents/"]


def _env(name: str, default: str | None = None) -> str | None:
    """Read NAME, or the contents of the file named by NAME_FILE (Docker secrets)."""
    file_var = os.environ.get(f"{name}_FILE")
    if file_var:
        return Path(file_var).read_text(encoding="utf-8").strip()
    return os.environ.get(name, default)


class Settings(BaseModel):
    gitlab_url: str
    bot_username: str
    mention: str = "@agent"
    webhook_secret: str
    ticket_secret: str
    database_url: str = "sqlite:///./orchestrator.db"
    # URL of this orchestrator as reachable from sandbox runner jobs.
    public_url: str
    sandbox_project: str
    sandbox_ref: str = "main"
    sandbox_token_ref: str | None = None  # token used to trigger sandbox pipelines; defaults to project token
    max_concurrent_sandboxes: int = 2
    agents_dir: str = "agents"
    projects_file: str = "config/projects.yaml"
    secrets_dir: str = "/run/secrets"
    tls_ca_file: str | None = None
    poll_interval_seconds: float = 5.0
    # How long a dispatched task may wait in the sandbox queue before it counts as timed out (on top of agent timeout).
    queue_grace_minutes: int = 30
    admin_token: str | None = None  # enables GET /api/tasks for operators
    # LLM used by inline agents (chat) that run inside the orchestrator. Unset: inline agents cannot run.
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str = "EMPTY"
    llm_ca_file: str | None = None
    chat_workers: int = 2  # concurrent inline runs; 0 runs them synchronously in the worker loop (tests)

    @classmethod
    def from_env(cls) -> "Settings":
        def req(name: str) -> str:
            value = _env(name)
            if not value:
                raise RuntimeError(f"missing required setting {name}")
            return value

        data: dict[str, Any] = {
            "gitlab_url": req("GITLAB_URL").rstrip("/"),
            "bot_username": req("BOT_USERNAME"),
            "webhook_secret": req("WEBHOOK_SECRET"),
            "ticket_secret": req("TICKET_SECRET"),
            "public_url": req("PUBLIC_URL").rstrip("/"),
            "sandbox_project": req("SANDBOX_PROJECT"),
        }
        optional = {
            "mention": "AGENT_MENTION",
            "database_url": "DATABASE_URL",
            "sandbox_ref": "SANDBOX_REF",
            "sandbox_token_ref": "SANDBOX_TOKEN_REF",
            "max_concurrent_sandboxes": "MAX_CONCURRENT_SANDBOXES",
            "agents_dir": "AGENTS_DIR",
            "projects_file": "PROJECTS_FILE",
            "secrets_dir": "SECRETS_DIR",
            "tls_ca_file": "TLS_CA_FILE",
            "poll_interval_seconds": "POLL_INTERVAL_SECONDS",
            "queue_grace_minutes": "QUEUE_GRACE_MINUTES",
            "admin_token": "ADMIN_TOKEN",
            "llm_base_url": "LLM_BASE_URL",
            "llm_model": "LLM_MODEL",
            "llm_api_key": "LLM_API_KEY",
            "llm_ca_file": "LLM_CA_FILE",
            "chat_workers": "CHAT_WORKERS",
        }
        for field, env_name in optional.items():
            value = _env(env_name)
            if value:
                data[field] = value
        return cls(**data)

    def read_secret(self, ref: str) -> str:
        path = Path(self.secrets_dir) / ref
        if not path.is_file():
            raise RuntimeError(f"secret {ref!r} not found in {self.secrets_dir}")
        return path.read_text(encoding="utf-8").strip()


class PipelineFixerConfig(BaseModel):
    branches: list[str] = Field(default_factory=lambda: ["*"])
    skip_stale: bool = True  # ignore failures for SHAs that are no longer the branch head


class CustomAgentsPolicy(BaseModel):
    """What repository-defined agents (.gitlab/agents/*.yaml) may do in a project. Off unless the admin enables it."""

    enabled: bool = False
    allow_edit: bool = False  # edit agents open MRs/commits; read-only agents only comment
    engines: list[str] = Field(default_factory=lambda: ["llm"])
    tools: list[str] = Field(default_factory=lambda: [
        "get_issue", "list_issue_notes", "search_issues", "get_merge_request", "get_mr_diff", "list_mr_discussions",
        "search_merge_requests", "list_pipelines", "get_pipeline_jobs", "get_job_log",
    ])
    max_agents: int = 10


class ProjectConfig(BaseModel):
    token_ref: str = "gitlab_bot_token"
    sandbox_image: str = ""
    setup: list[str] = Field(default_factory=list)
    test: list[str] = Field(default_factory=list)
    protected_paths: list[str] = Field(default_factory=lambda: [".gitlab/agent.yml"])
    agents: list[str] = Field(default_factory=lambda: ["pipeline-fixer"])
    allow_push_to_mr_branch: bool = False
    pipeline_fixer: PipelineFixerConfig = Field(default_factory=PipelineFixerConfig)
    custom_agents: CustomAgentsPolicy = Field(default_factory=CustomAgentsPolicy)

    @model_validator(mode="after")
    def _always_protected(self) -> "ProjectConfig":
        self.protected_paths = self.protected_paths + [p for p in ALWAYS_PROTECTED if p not in self.protected_paths]
        return self

    def branch_allowed(self, ref: str) -> bool:
        return any(fnmatch.fnmatchcase(ref, pattern) for pattern in self.pipeline_fixer.branches)

    def with_repo_overrides(self, repo_cfg: dict[str, Any], allowed_image_prefixes: list[str]) -> "ProjectConfig":
        """Merge a repository's .gitlab/agent.yml.

        The repo file is written by developers, so it may only narrow what the admin enabled:
        agents are intersected, protected paths are added to, and the image must come from an allowed registry.
        """
        merged = self.model_copy(deep=True)
        if not isinstance(repo_cfg, dict):
            return merged
        if isinstance(repo_cfg.get("setup"), list):
            merged.setup = [str(c) for c in repo_cfg["setup"]]
        if isinstance(repo_cfg.get("test"), list):
            merged.test = [str(c) for c in repo_cfg["test"]]
        if isinstance(repo_cfg.get("protected_paths"), list):
            merged.protected_paths = sorted(set(merged.protected_paths) | {str(p) for p in repo_cfg["protected_paths"]})
        if isinstance(repo_cfg.get("agents"), list):
            wanted = {str(a) for a in repo_cfg["agents"]}
            merged.agents = [a for a in merged.agents if a in wanted]
        custom = repo_cfg.get("custom_agents")
        if custom is False or (isinstance(custom, dict) and custom.get("enabled") is False):
            merged.custom_agents.enabled = False
        image = repo_cfg.get("sandbox_image")
        if isinstance(image, str) and image and any(image.startswith(p) for p in allowed_image_prefixes):
            merged.sandbox_image = image
        return merged


class ProjectsConfig(BaseModel):
    defaults: ProjectConfig = Field(default_factory=ProjectConfig)
    allowed_image_prefixes: list[str] = Field(default_factory=list)
    projects: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "ProjectsConfig":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls(**raw)

    def for_project(self, project_path: str) -> ProjectConfig | None:
        """Return the effective config, or None when the project is not onboarded."""
        if project_path not in self.projects:
            return None
        base = self.defaults.model_dump()
        override = self.projects[project_path] or {}
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                base[key] = {**base[key], **value}
            else:
                base[key] = value
        return ProjectConfig(**base)
