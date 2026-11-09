"""Match an Event against the agent registry. Pure logic: no I/O, so it is unit-testable."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import ProjectConfig
from .events import Event
from .registry import AgentDef, FlowDef, Registry


@dataclass
class Match:
    agent: AgentDef
    dedupe_key: str
    # Key used to count attempts (e.g. per branch for the pipeline fixer) and to allow one active task per object.
    subject_key: str
    command: str | None = None
    instruction: str = ""
    any_author: bool = False  # fired by a trigger that runs for authors without Developer access


@dataclass
class FlowMatch:
    flow: FlowDef
    dedupe_key: str
    subject_key: str
    instruction: str = ""


@dataclass
class RouteResult:
    matches: list[Match] = field(default_factory=list)
    flows: list[FlowMatch] = field(default_factory=list)
    reply: str | None = None  # a direct answer (help / unknown command), posted as a note
    needs_permission_check: bool = False  # human-initiated: verify the actor has Developer access
    reason: str = ""


def parse_mention(body: str, mention: str) -> tuple[str, str] | None:
    """Return (command, instruction) for the first `@agent <command> ...` in a comment, ignoring quoted lines and code."""
    lines = []
    in_fence = False
    for line in body.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or line.lstrip().startswith(">"):
            continue
        lines.append(line)
    text = "\n".join(lines)
    m = re.search(rf"(?<![\w`]){re.escape(mention)}\b[ \t]*([\w-]*)(.*)", text, re.DOTALL)
    if not m:
        return None
    return m.group(1).lower(), m.group(2).strip()


def route(event: Event, registry: Registry, project: ProjectConfig, bot_username: str, mention: str,
          custom: list[AgentDef] | None = None) -> RouteResult:
    """`custom` are the project's repository-defined agents, already validated against its policy."""
    enabled = [registry.agents[n] for n in project.agents if n in registry.agents] + list(custom or [])
    flows = registry.flows_for(project.agents)

    if event.kind == "pipeline":
        return _route_pipeline(event, enabled, project)

    # Never react to our own comments, labels or MRs: that is how agent loops start.
    if event.actor_username == bot_username:
        return RouteResult(reason="event authored by bot")

    if event.kind == "note":
        return _route_note(event, enabled, flows, registry, mention)
    if event.kind in ("issue", "merge_request"):
        return _route_object(event, enabled, flows)
    return RouteResult(reason=f"unhandled event kind {event.kind}")


def _route_pipeline(event: Event, enabled: list[AgentDef], project: ProjectConfig) -> RouteResult:
    result = RouteResult()
    if not event.ref:
        result.reason = "pipeline without a ref"
        return result
    for agent in enabled:
        for t in agent.triggers:
            if t.event != "pipeline" or t.status != event.pipeline_status:
                continue
            if agent.output == "security_fix":
                # Opt-in per project, and only on the configured (default) branches.
                policy = project.security_remediator
                if policy.auto and policy.branch_allowed(event.ref):
                    result.matches.append(Match(
                        agent=agent,
                        dedupe_key=f"{agent.name}:pipeline:{event.project_id}:{event.sha}",
                        subject_key=f"{event.project_id}:security:{event.ref}",
                    ))
            elif project.branch_allowed(event.ref):
                result.matches.append(Match(
                    agent=agent,
                    dedupe_key=f"{agent.name}:pipeline:{event.project_id}:{event.pipeline_id}",
                    subject_key=f"{event.project_id}:branch:{event.ref}",
                ))
            break
    if not result.matches:
        result.reason = f"no pipeline trigger for {event.pipeline_status} on {event.ref}"
    return result


def _route_note(event: Event, enabled: list[AgentDef], flows: list[FlowDef], registry: Registry,
                mention: str) -> RouteResult:
    parsed = parse_mention(event.note_body or "", mention)
    if not parsed:
        return RouteResult(reason="no mention")
    command, instruction = parsed
    result = RouteResult(needs_permission_check=True)
    if command in ("", "help"):
        result.reply = registry.help_text(mention, enabled, flows)
        return result
    subject_key = f"{event.project_id}:{event.noteable_type}:{event.iid or event.sha}"
    for flow in flows:
        for t in flow.triggers:
            if t.event == "note" and command in t.commands and event.noteable_type in t.targets:
                result.flows.append(FlowMatch(flow=flow, dedupe_key=f"flow:{flow.name}:note:{event.project_id}:{event.note_id}",
                                              subject_key=subject_key, instruction=instruction))
                return result
    for agent in enabled:
        for t in agent.triggers:
            if t.event == "note" and command in t.commands and event.noteable_type in t.targets:
                result.matches.append(_note_match(event, agent, subject_key, command, instruction))
                return result
    # Not a command: treat the whole message as a question for the chat agent, when it is enabled here.
    chat = next((a for a in enabled if a.output == "chat"
                 and any(t.event == "note" and event.noteable_type in t.targets for t in a.triggers)), None)
    if chat is not None:
        result.matches.append(_note_match(event, chat, subject_key, "chat", f"{command} {instruction}".strip()))
        return result
    result.reply = (
        f"I don't know the command `{command}` here, or it is not enabled for this project.\n\n"
        + registry.help_text(mention, enabled, flows)
    )
    return result


def _note_match(event: Event, agent: AgentDef, subject_key: str, command: str, instruction: str) -> Match:
    if agent.output == "chat":
        # A chat is a conversation per thread; several threads on one issue may chat at once.
        subject_key += f":chat:{event.discussion_id or event.note_id}"
    return Match(agent=agent, dedupe_key=f"{agent.name}:note:{event.project_id}:{event.note_id}",
                 subject_key=subject_key, command=command, instruction=instruction)


def _route_object(event: Event, enabled: list[AgentDef], flows: list[FlowDef]) -> RouteResult:
    result = RouteResult()
    for flow in flows:
        for t in flow.triggers:
            if t.event == "label" and t.label in event.added_labels and event.noteable_type in t.targets:
                result.flows.append(FlowMatch(
                    flow=flow, dedupe_key=f"flow:{flow.name}:{event.project_id}:{event.noteable_type}:{event.iid}:label",
                    subject_key=f"{event.project_id}:{event.noteable_type}:{event.iid}"))
    for agent in enabled:
        for t in agent.triggers:
            if event.noteable_type not in t.targets and t.event == "label":
                continue
            fired = False
            if t.event == "label" and t.label in event.added_labels:
                fired = True
                key = f"label:{t.label}"
            elif (
                t.event == "merge_request"
                and event.kind == "merge_request"
                and event.action in t.actions
                and (t.require_label is None or t.require_label in event.labels)
                # "update" also fires for title/label edits; only react when new commits were pushed.
                and (event.action != "update" or event.has_new_commits)
            ):
                fired = True
                key = f"mr:{event.action}:{event.sha}"
            elif (
                t.event == "issue"
                and event.kind == "issue"
                and event.action in t.actions
                and (t.require_label is None or t.require_label in event.labels)
            ):
                fired = True
                key = f"issue:{event.action}"
            if fired:
                result.matches.append(Match(
                    agent=agent,
                    dedupe_key=f"{agent.name}:{event.project_id}:{event.noteable_type}:{event.iid}:{key}",
                    subject_key=f"{event.project_id}:{event.noteable_type}:{event.iid}",
                    any_author=t.allow_any_author,
                ))
                break
    # Labels and flows need Developer access; so does any agent whose trigger does not allow any author.
    result.needs_permission_check = bool(result.flows) or any(not m.any_author for m in result.matches)
    if not result.matches and not result.flows:
        result.reason = "no trigger matched"
    return result
