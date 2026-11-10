"""Repository-defined agents: .gitlab/agents/*.yaml on a project's default branch.

Only the default branch is read, so a merge request cannot add or change an agent before it is merged.
Every definition is checked against the project's CustomAgentsPolicy (set by the platform admin) and must not
collide with the built-in catalog. Invalid files are skipped and reported in `errors`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import yaml
from pydantic import ValidationError

from .config import CustomAgentsPolicy
from .gitlab_client import GitLab
from .registry import AgentDef, CustomAgentDef, Registry
from .repofiles import DefaultBranchFiles

log = logging.getLogger("orchestrator.catalog")

AGENTS_DIR = ".gitlab/agents"


@dataclass
class RepoCatalog:
    sha: str
    agents: list[AgentDef] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)  # file -> why it was skipped


def check_policy(agent: CustomAgentDef, policy: CustomAgentsPolicy, registry: Registry, taken: set[str]) -> str | None:
    """Return why the agent is not allowed in this project, or None."""
    if agent.mode == "edit" and not policy.allow_edit:
        return "edit agents are not allowed in this project"
    if agent.engine not in policy.engines:
        return f"engine {agent.engine} is not allowed (allowed: {', '.join(policy.engines)})"
    tools = set(agent.tools) - set(policy.tools)
    if tools:
        return f"tool(s) not allowed: {', '.join(sorted(tools))}"
    commands = {c for t in agent.triggers for c in t.commands}
    clash = commands & (registry.commands() | taken)
    if clash:
        return f"command(s) already used: {', '.join(sorted(clash))}"
    labels = {t.label for t in agent.triggers if t.label} & registry.labels()
    if labels:
        return f"label(s) reserved by built-in agents: {', '.join(sorted(labels))}"
    return None


def parse(files: dict[str, str], policy: CustomAgentsPolicy, registry: Registry, sha: str) -> RepoCatalog:
    """Validate the YAML files (path -> text) of one repository."""
    catalog = RepoCatalog(sha=sha)
    taken: set[str] = set()
    for path in sorted(files)[: policy.max_agents]:
        try:
            raw = yaml.safe_load(files[path])
            agent = CustomAgentDef(**(raw if isinstance(raw, dict) else {}))
        except (yaml.YAMLError, ValidationError, TypeError) as exc:
            catalog.errors[path] = str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__
            continue
        problem = check_policy(agent, policy, registry, taken)
        if problem:
            catalog.errors[path] = problem
            continue
        taken |= {c for t in agent.triggers for c in t.commands}
        catalog.agents.append(agent.to_agent())
    for path in sorted(files)[policy.max_agents:]:
        catalog.errors[path] = f"more than {policy.max_agents} agent files"
    return catalog


class CatalogCache:
    """Per-project cache keyed by the default branch head (see repofiles.DefaultBranchFiles)."""

    def __init__(self, registry: Registry, files: DefaultBranchFiles | None = None):
        self.registry = registry
        self.files = files or DefaultBranchFiles()
        self._cache: dict[int, RepoCatalog] = {}

    def get(self, gl: GitLab, project_id: int, policy: CustomAgentsPolicy) -> RepoCatalog:
        if not policy.enabled:
            return RepoCatalog(sha="")
        sha = self.files.head(gl, project_id)
        cached = self._cache.get(project_id)
        if cached and cached.sha == sha:
            return cached
        files: dict[str, str] = {}
        if sha:
            for entry in gl.repository_tree(project_id, AGENTS_DIR, sha):
                if entry.get("type") == "blob" and entry["path"].endswith((".yaml", ".yml")):
                    text = gl.raw_file(project_id, entry["path"], sha)
                    if text is not None:
                        files[entry["path"]] = text
        catalog = parse(files, policy, self.registry, sha)
        for path, why in catalog.errors.items():
            log.warning("project %s: skipped custom agent %s: %s", project_id, path, why)
        self._cache[project_id] = catalog
        return catalog
