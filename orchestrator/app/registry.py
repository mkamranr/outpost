"""Declarative agent catalog loaded from agents/*.yaml."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from jinja2 import FileSystemLoader, StrictUndefined
from jinja2.sandbox import SandboxedEnvironment
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .tools import TOOLS

Noteable = Literal["issue", "merge_request", "commit"]


class Trigger(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a typo in an agent file should fail loudly, not be ignored

    event: Literal["pipeline", "note", "label", "merge_request"]
    # pipeline
    status: str | None = None
    # note: "@agent <command>"
    commands: list[str] = Field(default_factory=list)
    targets: list[Noteable] = Field(default_factory=lambda: ["issue", "merge_request"])
    # label: fires when this label is added
    label: str | None = None
    # merge_request: actions such as open/update/reopen, optionally gated by a label already on the MR
    actions: list[str] = Field(default_factory=list)
    require_label: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "Trigger":
        if self.event == "note" and not self.commands:
            raise ValueError("note triggers need at least one command")
        if self.event == "label" and not self.label:
            raise ValueError("label triggers need a label")
        return self


class AgentDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    phase: int = 1
    mode: Literal["edit", "readonly"]
    # inline: runs inside the orchestrator with GitLab tools only (no sandbox, no checkout); built-in agents only.
    engine: Literal["aider", "mini-swe", "llm", "inline"]
    output: Literal["pipeline_fix", "merge_request", "note", "review", "mr_commit", "chat"]
    triggers: list[Trigger]
    prompts: dict[str, str]  # role -> template file in agents/prompts
    context: list[str] = Field(default_factory=list)  # collector names
    context_budget_tokens: int = 30000
    tools: list[str] = Field(default_factory=list)  # GitLab tools the agent may call while it runs (app/tools)
    max_attempts: int = 3
    timeout_minutes: int = 30
    classify: bool = False
    allow_ci_changes: bool = False
    # Note outputs: an HTML-comment marker other agents can find (e.g. the planner's plan) and a closing line.
    note_marker: str | None = None
    note_suffix: str = ""
    # builtin: agents/*.yaml, prompts are template files. repo: .gitlab/agents/*.yaml, prompts are template text.
    source: Literal["builtin", "repo"] = "builtin"

    @model_validator(mode="after")
    def _check(self) -> "AgentDef":
        if "main" not in self.prompts:
            raise ValueError(f"agent {self.name}: prompts.main is required")
        if self.classify and "classify" not in self.prompts:
            raise ValueError(f"agent {self.name}: classify=true needs prompts.classify")
        if self.mode == "readonly" and self.engine not in ("llm", "inline"):
            raise ValueError(f"agent {self.name}: readonly agents must use the llm or inline engine")
        if self.engine == "inline" and (self.mode != "readonly" or self.source != "builtin"):
            raise ValueError(f"agent {self.name}: only built-in read-only agents can run inline")
        return self

    @property
    def commands(self) -> list[str]:
        return [c for t in self.triggers if t.event == "note" for c in t.commands]


RESERVED_COMMANDS = {"help", "chat", "approve", "stop"}
APPROVAL_LABEL = "agent::approved"
MAX_FLOW_STEPS = 10
MR_OUTPUTS = {"merge_request", "mr_commit", "pipeline_fix"}  # outputs that leave an MR behind for the next step
CUSTOM_CONTEXT = {"issue", "issue_notes", "mr", "mr_diff", "discussion"}  # collectors safe for any trigger target


class CustomAgentDef(BaseModel):
    """A repository-defined agent (.gitlab/agents/<name>.yaml). Deliberately narrower than AgentDef:
    no pipeline triggers, no CI changes, no note markers (those make a note trusted by other agents)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,39}$")
    description: str = Field(max_length=300)
    mode: Literal["edit", "readonly"] = "readonly"
    engine: Literal["aider", "mini-swe", "llm"] = "llm"
    output: Literal["note", "review", "merge_request", "mr_commit"] = "note"
    prompt: str = Field(min_length=20, max_length=20000)
    triggers: list[Trigger] = Field(min_length=1)
    context: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    context_budget_tokens: int = Field(default=24000, ge=2000, le=40000)
    max_attempts: int = Field(default=2, ge=1, le=3)
    timeout_minutes: int = Field(default=20, ge=5, le=45)

    @model_validator(mode="after")
    def _check(self) -> "CustomAgentDef":
        if any(t.event not in ("note", "label") for t in self.triggers):
            raise ValueError("custom agents can only be triggered by commands or labels")
        unknown = set(self.context) - CUSTOM_CONTEXT
        if unknown:
            raise ValueError(f"context not available to custom agents: {', '.join(sorted(unknown))}")
        targets = {o for t in self.triggers for o in t.targets}
        needs = {"review": {"merge_request"}, "mr_commit": {"merge_request"}, "merge_request": {"issue"}}.get(self.output)
        if needs and not targets <= needs:
            raise ValueError(f"output {self.output} only works on {', '.join(needs)} targets")
        return self

    def to_agent(self) -> AgentDef:
        return AgentDef(
            name=f"custom/{self.name}", description=self.description, mode=self.mode, engine=self.engine,
            output=self.output, triggers=self.triggers, prompts={"main": self.prompt}, context=self.context,
            tools=self.tools, context_budget_tokens=self.context_budget_tokens, max_attempts=self.max_attempts,
            timeout_minutes=self.timeout_minutes, source="repo", phase=0,
        )


class FlowStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: str | list[str] | None = None  # a list runs the agents in parallel
    gate: Literal["approval"] | None = None  # wait for "@agent approve" or the agent::approved label
    target: Literal["subject", "produced_mr"] = "subject"  # produced_mr: the MR the previous step opened
    when: Literal["success", "always"] = "success"  # run only if every task of the previous step succeeded

    @model_validator(mode="after")
    def _check(self) -> "FlowStep":
        if (self.agent is None) == (self.gate is None):
            raise ValueError("a flow step is either an agent (or list of agents) or a gate")
        return self

    @property
    def agents(self) -> list[str]:
        if self.agent is None:
            return []
        return [self.agent] if isinstance(self.agent, str) else list(self.agent)

    def label(self) -> str:
        return "approval" if self.gate else " + ".join(f"`{a}`" for a in self.agents)


class FlowDef(BaseModel):
    """A fixed sequence of agents with optional human gates (agents/flows/*.yaml)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    triggers: list[Trigger]
    steps: list[FlowStep] = Field(min_length=1, max_length=MAX_FLOW_STEPS)

    @model_validator(mode="after")
    def _check(self) -> "FlowDef":
        if any(t.event not in ("note", "label") for t in self.triggers):
            raise ValueError(f"flow {self.name}: flows are started by commands or labels")
        if self.steps[0].gate or self.steps[-1].gate:
            raise ValueError(f"flow {self.name}: a flow cannot start or end with a gate")
        return self

    @property
    def agent_names(self) -> set[str]:
        return {a for step in self.steps for a in step.agents}

    @property
    def commands(self) -> list[str]:
        return [c for t in self.triggers if t.event == "note" for c in t.commands]


class Registry:
    def __init__(self, agents: dict[str, AgentDef], prompts_dir: Path, flows: dict[str, FlowDef] | None = None):
        self.agents = agents
        self.flows = flows or {}
        self._env = SandboxedEnvironment(
            loader=FileSystemLoader(str(prompts_dir)),
            undefined=StrictUndefined,
            autoescape=False,
            keep_trailing_newline=True,
        )

    @classmethod
    def load(cls, agents_dir: str | Path) -> "Registry":
        agents_dir = Path(agents_dir)
        agents: dict[str, AgentDef] = {}
        for path in sorted(agents_dir.glob("*.yaml")):
            agent = AgentDef(**yaml.safe_load(path.read_text(encoding="utf-8")))
            if agent.name in agents:
                raise ValueError(f"duplicate agent name {agent.name}")
            agents[agent.name] = agent
        prompts_dir = agents_dir / "prompts"
        for agent in agents.values():
            for template in agent.prompts.values():
                if not (prompts_dir / template).is_file():
                    raise ValueError(f"agent {agent.name}: prompt template {template} not found")
            unknown = [t for t in agent.tools if t not in TOOLS]
            if unknown:
                raise ValueError(f"agent {agent.name}: unknown tool(s) {', '.join(unknown)}")
        seen: dict[tuple[str, str], str] = {}
        for agent in agents.values():
            for trigger in (t for t in agent.triggers if t.event == "note"):
                for command in trigger.commands:
                    for noteable in trigger.targets:
                        key = (command, noteable)
                        if key in seen:
                            raise ValueError(f"command {command!r} on {noteable} claimed by {seen[key]} and {agent.name}")
                        seen[key] = agent.name
        flows = cls._load_flows(agents_dir / "flows", agents, seen)
        return cls(agents, prompts_dir, flows)

    @staticmethod
    def _load_flows(flows_dir: Path, agents: dict[str, AgentDef], seen: dict[tuple[str, str], str]) -> dict[str, FlowDef]:
        flows: dict[str, FlowDef] = {}
        for path in sorted(flows_dir.glob("*.yaml")) if flows_dir.is_dir() else []:
            flow = FlowDef(**yaml.safe_load(path.read_text(encoding="utf-8")))
            if flow.name in flows or flow.name in agents:
                raise ValueError(f"duplicate flow name {flow.name}")
            missing = flow.agent_names - set(agents)
            if missing:
                raise ValueError(f"flow {flow.name}: unknown agent(s) {', '.join(sorted(missing))}")
            for i, step in enumerate(flow.steps):
                if step.target != "produced_mr":
                    continue
                before = next((s for s in reversed(flow.steps[:i]) if s.agent), None)
                if before is None or not any(agents[a].output in MR_OUTPUTS for a in before.agents):
                    raise ValueError(f"flow {flow.name}: step {i + 1} targets produced_mr but the step before "
                                     "it does not open or update a merge request")
            for trigger in (t for t in flow.triggers if t.event == "note"):
                for command in trigger.commands:
                    if command in RESERVED_COMMANDS:
                        raise ValueError(f"flow {flow.name}: command {command!r} is reserved")
                    for noteable in trigger.targets:
                        if (command, noteable) in seen:
                            raise ValueError(f"command {command!r} on {noteable} claimed by {seen[(command, noteable)]} "
                                             f"and flow {flow.name}")
                        seen[(command, noteable)] = f"flow {flow.name}"
            flows[flow.name] = flow
        return flows

    def get(self, name: str) -> AgentDef:
        return self.agents[name]

    def render(self, agent_def: AgentDef, role: str, variables: dict) -> str:
        if agent_def.source == "repo":
            # Repository-defined prompts always get the untrusted-data preamble first.
            template = self._env.from_string('{% include "_untrusted.md.j2" %}\n' + agent_def.prompts[role])
            return template.render(**variables)
        return self._env.get_template(agent_def.prompts[role]).render(**variables)

    def commands(self) -> set[str]:
        flow_commands = {c for f in self.flows.values() for c in f.commands}
        return {c for a in self.agents.values() for c in a.commands} | flow_commands | RESERVED_COMMANDS

    def labels(self) -> set[str]:
        defs: list[AgentDef | FlowDef] = [*self.agents.values(), *self.flows.values()]
        return {t.label for d in defs for t in d.triggers if t.label} | {APPROVAL_LABEL}

    def flows_for(self, enabled: list[str]) -> list[FlowDef]:
        """Flows usable in a project: every agent they need is enabled there."""
        return [f for f in self.flows.values() if f.agent_names <= set(enabled)]

    @staticmethod
    def _how(triggers: list[Trigger], mention: str) -> str:
        how = []
        for t in triggers:
            if t.event == "note":
                how += [f"`{mention} {c}` on {'/'.join(o.replace('_', ' ') for o in t.targets)}" for c in t.commands]
            elif t.event == "label":
                how.append(f"label `{t.label}`")
            elif t.event == "pipeline":
                how.append(f"automatic on pipeline `{t.status}`")
            elif t.event == "merge_request":
                gate = f" with label `{t.require_label}`" if t.require_label else ""
                how.append(f"automatic on MR {'/'.join(t.actions)}{gate}")
        return "<br>".join(how)

    def help_text(self, mention: str, agents: list[AgentDef], flows: list[FlowDef] | None = None) -> str:
        lines = ["**Available agents**", "", "| Agent | How to invoke | What it does |", "| --- | --- | --- |"]
        for agent in agents:
            lines.append(f"| `{agent.name}` | {self._how(agent.triggers, mention)} | {agent.description} |")
        if flows:
            lines += ["", "**Flows** (several agents in sequence)", "", "| Flow | How to start | Steps |", "| --- | --- | --- |"]
            for flow in flows:
                steps = " → ".join(s.label() for s in flow.steps)
                lines.append(f"| `{flow.name}` | {self._how(flow.triggers, mention)} | {flow.description}<br>{steps} |")
        if any(a.output == "chat" for a in agents):
            lines += ["", f"Anything else after `{mention}` is answered by the `chat` agent in the same thread."]
        if flows:
            lines += ["", f"`{mention} stop` stops a running flow."]
        return "\n".join(lines)
