"""The orchestrator's core: turn events into tasks, dispatch tasks to the sandbox, apply results."""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any, Callable

import yaml

from . import feedback, flows, inline, outputs, tickets, tools
from .catalog import CatalogCache, RepoCatalog
from .collectors import CollectContext, collect
from .config import ProjectConfig, ProjectsConfig, Settings
from .events import Event
from .gitlab_client import DEVELOPER, GitLab
from .outputs.common import OutputContext, sanitize
from .registry import AgentDef, Registry
from .repofiles import INSTRUCTIONS_FILE, DefaultBranchFiles, instructions_block
from .router import route
from .tasks import (ACTIVE, DISPATCHED, DISPATCHING, DONE, FAILED, QUEUED, RESULT_RECEIVED, Task, TaskStore,
                    utcnow)

log = logging.getLogger("orchestrator")

GitLabFactory = Callable[[str], GitLab]
TOOL_CALL_CAP = 60  # per task: a loop that needs more is lost, not thorough


class Platform:
    def __init__(self, settings: Settings, registry: Registry, projects: ProjectsConfig, store: TaskStore,
                 gitlab_factory: GitLabFactory | None = None, llm_factory: inline.LLMFactory | None = None):
        self.settings = settings
        self.registry = registry
        self.projects = projects
        self.store = store
        self._factory = gitlab_factory or (
            lambda ref: GitLab(settings.gitlab_url, settings.read_secret(ref), verify=settings.tls_ca_file or True)
        )
        self._clients: dict[str, GitLab] = {}
        self.repo_files = DefaultBranchFiles()
        self.catalog = CatalogCache(registry, self.repo_files)
        self.flows = flows.FlowEngine(self)
        self._llm = llm_factory or inline.llm_factory(settings)
        self._inline_pool = ThreadPoolExecutor(settings.chat_workers, "inline") if settings.chat_workers > 0 else None
        # Inline agents get their own capacity so a question is not queued behind long sandbox jobs.
        self.inline_agents = frozenset(n for n, a in registry.agents.items() if a.engine == "inline")
        self.reactions = feedback.ReactionPoller(self)

    # -- clients ------------------------------------------------------------------
    def gitlab(self, token_ref: str) -> GitLab:
        if token_ref not in self._clients:
            self._clients[token_ref] = self._factory(token_ref)
        return self._clients[token_ref]

    def project_gl(self, cfg: ProjectConfig) -> GitLab:
        return self.gitlab(cfg.token_ref)

    def sandbox_gl(self, cfg: ProjectConfig) -> GitLab:
        return self.gitlab(self.settings.sandbox_token_ref or cfg.token_ref)

    # -- agents -------------------------------------------------------------------
    def agent_for(self, task: Task) -> AgentDef:
        """The definition the task was created with (custom agents may have changed or gone since)."""
        if task.agent_json:
            return AgentDef.model_validate_json(task.agent_json)
        return self.registry.get(task.agent)

    def enabled_catalog(self, project_id: int, cfg: ProjectConfig) -> list[dict[str, str]]:
        """Agents and flows a project can hand work to (what the chat agent may route to)."""
        items = [{"name": a.name, "kind": "agent (read-only)" if a.mode == "readonly" else "agent (changes code)",
                  "description": a.description}
                 for n in cfg.agents if (a := self.registry.agents.get(n)) and a.output != "chat"]
        try:
            custom = self.catalog.get(self.project_gl(cfg), project_id, cfg.custom_agents).agents
        except Exception:  # noqa: BLE001
            custom = []
        items += [{"name": a.name, "kind": "custom agent", "description": a.description} for a in custom]
        items += [{"name": f.name, "kind": "flow", "description": f.description} for f in self.registry.flows_for(cfg.agents)]
        return items

    def custom_catalog(self, event: Event, cfg: ProjectConfig) -> RepoCatalog:
        if event.kind == "pipeline" or event.actor_username == self.settings.bot_username:
            return RepoCatalog(sha="")
        try:
            return self.catalog.get(self.project_gl(cfg), event.project_id, cfg.custom_agents)
        except Exception:  # noqa: BLE001 - a broken custom catalog must not take the built-in agents down
            log.exception("could not load custom agents for %s", event.project_path)
            return RepoCatalog(sha="")

    # -- events -------------------------------------------------------------------
    def handle_event(self, event: Event) -> dict[str, Any]:
        """Route a webhook event and enqueue tasks. Returns a summary for logs and tests."""
        if event.project_path == self.settings.sandbox_project or str(event.project_id) == self.settings.sandbox_project:
            return {"ignored": "sandbox project"}
        cfg = self.projects.for_project(event.project_path)
        if cfg is None:
            return {"ignored": "project not onboarded"}
        try:  # before routing: merges, closes and green pipelines on agent branches are never routed
            recorded = feedback.record_outcome(self, event)
            if recorded:
                log.info("feedback %s recorded from %s event", recorded, event.kind)
        except Exception:  # noqa: BLE001 - feedback must not block routing
            log.exception("recording outcome feedback failed")
        if event.actor_username != self.settings.bot_username:
            ctl = flows.control(event, self.settings.mention)
            if ctl:
                handled = self.flows.handle_control(event, *ctl)
                if handled is not None:
                    return handled
                if ctl[0] == "stop":
                    self._reply(self.project_gl(cfg), event, "No flow is running here.")
                    return {"replied": True}
        custom = self.custom_catalog(event, cfg)
        result = route(event, self.registry, cfg, self.settings.bot_username, self.settings.mention, custom.agents)
        if not result.matches and not result.flows and not result.reply:
            return {"ignored": result.reason}
        gl = self.project_gl(cfg)

        if result.needs_permission_check:
            if not event.actor_id or gl.member_access_level(event.project_id, event.actor_id) < DEVELOPER:
                # Agents whose trigger allows any author (issue triage) still run; nothing else does.
                anyone = [m for m in result.matches if m.any_author]
                if not anyone or result.reply:
                    log.info("ignoring %s from %s: needs Developer access", event.kind, event.actor_username)
                    return {"ignored": "actor lacks Developer access"}
                result.matches, result.flows = anyone, []

        if result.reply:
            reply = result.reply
            if custom.errors:  # tell agent authors why their agent is missing from the list
                reply += "\n\n**Custom agents that were skipped:**\n" + "\n".join(
                    f"- `{path}`: {why}" for path, why in custom.errors.items())
            self._reply(gl, event, reply)
            return {"replied": True}

        created = []
        for flow_match in result.flows:
            started = self.flows.start(flow_match, event)
            created.append(started)
            if "run" in started:
                self._acknowledge(gl, event)
        for match in result.matches:
            agent = match.agent
            if self.store.count_for_subject(agent.name, match.subject_key, ACTIVE):
                created.append({"agent": agent.name, "skipped": "already working on this"})
                continue
            if agent.max_per_hour is not None and self.store.count_since(
                    agent.name, event.project_id, utcnow() - timedelta(hours=1)) >= agent.max_per_hour:
                log.warning("%s: hourly limit (%d) reached in %s", agent.name, agent.max_per_hour, event.project_path)
                created.append({"agent": agent.name, "skipped": "hourly limit"})
                continue
            if event.kind == "pipeline" and (event.ref or "").startswith("agent/"):
                # On our own branches the pipeline fixer iterates; cap it so a stubborn failure ends with a comment.
                tries = self.store.count_for_subject(agent.name, match.subject_key)
                if tries >= agent.max_attempts:
                    # Record the event as a finished task so the "giving up" note is posted exactly once.
                    marker = self.store.create(agent=agent.name, project_id=event.project_id,
                                               project_path=event.project_path, dedupe_key=match.dedupe_key,
                                               subject_key=match.subject_key, target=event.target())
                    if marker:
                        self.store.update(marker.id, status=DONE, outcome="attempt cap reached")
                        if tries == agent.max_attempts:
                            self._reply(gl, event, f"Pipeline #{event.pipeline_id} on `{event.ref}` still fails after "
                                                   f"{tries} fix attempts. Stopping here; a human needs to take over.")
                    created.append({"agent": agent.name, "skipped": "attempt cap"})
                    continue
            task = self.store.create(
                agent=agent.name, project_id=event.project_id, project_path=event.project_path,
                dedupe_key=match.dedupe_key, subject_key=match.subject_key, target=event.target(),
                command=match.command, instruction=match.instruction, agent_json=agent.model_dump_json(),
            )
            if task is None:
                created.append({"agent": agent.name, "skipped": "duplicate event"})
                continue
            created.append({"agent": agent.name, "task": task.id})
            self._acknowledge(gl, event)
        return {"tasks": created}

    def _acknowledge(self, gl: GitLab, event: Event) -> None:
        if event.kind == "note" and event.note_id and event.noteable_type in ("issue", "merge_request"):
            try:
                gl.award_note_emoji(event.project_id, event.noteable_type, event.iid, event.note_id, "eyes")
            except Exception:  # noqa: BLE001 - acknowledgement is cosmetic
                pass

    def _reply(self, gl: GitLab, event: Event, body: str) -> None:
        body = sanitize(body)
        if event.kind == "note" and event.discussion_id and event.noteable_type in ("issue", "merge_request"):
            gl.reply_to_discussion(event.project_id, event.noteable_type, event.iid, event.discussion_id, body)
        elif event.noteable_type in ("issue", "merge_request") and event.iid:
            gl.create_note(event.project_id, event.noteable_type, event.iid, body)
        elif event.sha:
            gl.create_commit_comment(event.project_id, event.sha, body)

    # -- dispatch -----------------------------------------------------------------
    def _resolve_repo(self, gl: GitLab, task: Task, project: dict[str, Any]) -> tuple[str, str]:
        t = task.target
        if t["kind"] == "pipeline":
            return t["ref"], t["sha"]
        if t.get("noteable_type") == "merge_request":
            mr = gl.merge_request(t["project_id"], t["iid"])
            return mr["source_branch"], mr["sha"]
        if t.get("noteable_type") == "commit" and t.get("sha"):
            return project["default_branch"], t["sha"]
        ref = project["default_branch"]
        return ref, gl.branch(t["project_id"], ref)["commit"]["id"]

    def _attach_pipeline(self, gl: GitLab, task: Task) -> str | None:
        """For `@agent fix-pipeline` comments: find the latest failed pipeline and pin it on the task target."""
        t = task.target
        if t.get("pipeline_id"):
            return None
        if t.get("noteable_type") == "merge_request":
            mr = gl.merge_request(t["project_id"], t["iid"])
            pipe = mr.get("head_pipeline") or {}
            ref, sha = mr["source_branch"], mr["sha"]
        else:
            ref, sha = None, t.get("sha")
            found = gl.pipelines(t["project_id"], sha=sha, per_page=1, order_by="id", sort="desc") if sha else []
            pipe = found[0] if found else {}
            ref = pipe.get("ref")
        if not pipe or pipe.get("status") != "failed":
            return "no failed pipeline found for this " + ("merge request" if t.get("iid") else "commit")
        jobs = gl.pipeline_jobs(t["project_id"], pipe["id"], scope="failed")
        t.update(kind="pipeline", pipeline_id=pipe["id"], ref=ref or pipe.get("ref"), sha=sha or pipe.get("sha"),
                 failed_job_ids=[j["id"] for j in jobs if not j.get("allow_failure")])
        self.store.update(task.id, target_json=json.dumps(t))
        task.target_json = json.dumps(t)
        return None

    def build_bundle(self, task: Task, agent: AgentDef, base_cfg: ProjectConfig) -> tuple[dict[str, Any] | None, ProjectConfig, str]:
        """Collect context and render prompts. Returns (bundle or None if the task should be skipped, cfg, reason)."""
        gl = self.project_gl(base_cfg)
        if agent.output == "pipeline_fix":
            problem = self._attach_pipeline(gl, task)
            if problem:
                return None, base_cfg, problem
        t = task.target
        project = gl.project(t["project_id"])
        ref, sha = self._resolve_repo(gl, task, project)

        if t["kind"] == "pipeline" and base_cfg.pipeline_fixer.skip_stale and not ref.startswith("agent/"):
            head = gl.branch(t["project_id"], ref)
            if head and head["commit"]["id"] != sha:
                return None, base_cfg, f"stale: {ref} has moved past {sha[:8]}"

        repo_cfg_text = gl.raw_file(t["project_id"], ".gitlab/agent.yml", sha)
        cfg = base_cfg
        if repo_cfg_text:
            try:
                cfg = base_cfg.with_repo_overrides(yaml.safe_load(repo_cfg_text) or {}, self.projects.allowed_image_prefixes)
            except yaml.YAMLError:
                pass
        if agent.source == "builtin" and agent.name not in cfg.agents:
            return None, cfg, "agent disabled by .gitlab/agent.yml"
        if agent.source == "repo" and not cfg.custom_agents.enabled:
            return None, cfg, "custom agents are disabled for this project"
        if not cfg.sandbox_image and agent.engine != "inline":
            raise RuntimeError("no sandbox_image configured for this project")
        if agent.output == "mr_description" and task.command is None and cfg.mr_describer.on_open == "only_if_empty":
            # Automatic run on MR open: leave descriptions the author wrote alone (and spend no GPU on them).
            current = gl.merge_request(t["project_id"], t["iid"]).get("description") or ""
            if not outputs.description_is_empty(current):
                return None, cfg, "the merge request already has a description"

        automatic = task.command is None and not task.flow_run_id
        if agent.output == "security_fix" and automatic:
            policy = cfg.security_remediator
            open_mrs = gl.merge_requests(t["project_id"], state="opened", labels=outputs.SECURITY_LABEL, per_page=100)
            if len(open_mrs) >= policy.max_open_mrs:
                return None, cfg, f"{len(open_mrs)} security fix MRs are already open"

        context = collect(agent.context, CollectContext(
            gl=gl, target=t, project=cfg, sha=sha, ref=ref,
            budget_tokens=agent.context_budget_tokens, bot_username=self.settings.bot_username,
            instruction=task.instruction,
        ))
        vulns = context.get("vulnerabilities")
        if agent.output == "security_fix" and not task.flow_run_id and isinstance(vulns, dict) \
                and not vulns.get("findings") and not vulns.get("error"):
            # Nothing to fix: do not start a sandbox. (In a flow, the security analyst's report is the input.)
            where = f"pipeline #{vulns['pipeline_id']}" if vulns.get("pipeline_id") else "this merge request"
            return None, cfg, (f"no security findings at or above `{cfg.security_remediator.min_severity}` in "
                               f"{where}" + (f" ({vulns['note']})" if vulns.get("note") else ""))
        variables = {
            "context": context,
            "target": t,
            "instruction": task.instruction,
            "repo": {"path": project["path_with_namespace"], "ref": ref, "sha": sha},
            "project": cfg.model_dump(),
            "agent": {"name": agent.name, "max_attempts": agent.max_attempts},
            "bot_username": self.settings.bot_username,
            "catalog": self.enabled_catalog(t["project_id"], cfg) if agent.output == "chat" else [],
        }
        prompts = {role: self.registry.render(agent, role, variables) for role in agent.prompts}
        prompts["main"] += self.team_instructions(gl, t["project_id"]) + self.flows.context_for(task)
        bundle = {
            "task_id": task.id,
            "agent": {
                "name": agent.name,
                "mode": agent.mode,
                "engine": agent.engine,
                "output": agent.output,
                "classify": agent.classify,
                "max_attempts": agent.max_attempts,
                "timeout_minutes": agent.timeout_minutes,
                "allow_ci_changes": agent.allow_ci_changes,
            },
            "prompts": prompts,
            "repo": {"path": project["path_with_namespace"], "http_url": project["http_url_to_repo"], "ref": ref, "sha": sha},
            "project": {"setup": cfg.setup, "test": cfg.test, "protected_paths": cfg.protected_paths},
            "context": context,
            "tools": tools.schemas(agent.tools),
        }
        return bundle, cfg, ""

    def team_instructions(self, gl: GitLab, project_id: int) -> str:
        """`.gitlab/agent-instructions.md` from the default branch, as a prompt section (best effort)."""
        try:
            return instructions_block(self.repo_files.text(gl, project_id, INSTRUCTIONS_FILE))
        except Exception:  # noqa: BLE001 - instructions are optional context
            log.warning("project %s: could not read %s", project_id, INSTRUCTIONS_FILE, exc_info=True)
            return ""

    def dispatch(self, task: Task) -> None:
        agent = self.agent_for(task)
        base_cfg = self.projects.for_project(task.project_path)
        if base_cfg is None:
            self.store.update(task.id, status=DONE, outcome="project no longer onboarded")
            self._finished(task.id)
            return
        try:
            bundle, cfg, reason = self.build_bundle(task, agent, base_cfg)
            if bundle is None:
                self.store.update(task.id, status=DONE, outcome=f"skipped: {reason}")
                if task.command:  # a human asked explicitly; tell them why nothing happens
                    self._notify_failure(task, f"Not running `{agent.name}`: {reason}.")
                self._finished(task.id)
                return
            if agent.engine == "inline":
                self._dispatch_inline(task, agent, bundle)
                return
            ttl = (agent.timeout_minutes + self.settings.queue_grace_minutes) * 60
            ticket = tickets.issue(self.settings.ticket_secret, task.id, ttl)
            pipeline = self.sandbox_gl(cfg).trigger_pipeline(self.settings.sandbox_project, self.settings.sandbox_ref, {
                "AGENT_TASK_ID": task.id,
                "AGENT_TASK_TICKET": ticket,
                "AGENT_ORCHESTRATOR_URL": self.settings.public_url,
                "AGENT_SANDBOX_IMAGE": cfg.sandbox_image,
                "AGENT_NAME": agent.name,
                "AGENT_TIMEOUT_MINUTES": str(agent.timeout_minutes),
            })
            self.store.update(
                task.id, status=DISPATCHED, bundle=bundle, sandbox_pipeline_id=pipeline["id"],
                deadline=self.store.deadline_for(ttl // 60),
            )
            log.info("task %s (%s) dispatched as sandbox pipeline %s", task.id, agent.name, pipeline["id"])
        except Exception as exc:  # noqa: BLE001
            log.exception("dispatch failed for task %s", task.id)
            self.store.update(task.id, status=FAILED, error=str(exc)[:2000])
            self._notify_failure(task, f"I could not start the `{agent.name}` agent: {str(exc)[:300]}")
            self._finished(task.id)

    def _dispatch_inline(self, task: Task, agent: AgentDef, bundle: dict[str, Any]) -> None:
        self.store.update(task.id, status=DISPATCHED, bundle=bundle,
                          deadline=self.store.deadline_for(agent.timeout_minutes + 1))
        task = self.store.get(task.id)
        if self._inline_pool is None:
            self._run_inline(task, bundle)
        else:
            self._inline_pool.submit(self._run_inline, task, bundle)
        log.info("task %s (%s) running inline", task.id, agent.name)

    def _run_inline(self, task: Task, bundle: dict[str, Any]) -> None:
        try:
            result = inline.run(self._llm(), bundle, lambda name, args: self.call_tool(task, name, args))
        except Exception as exc:  # noqa: BLE001 - always record an outcome
            log.exception("inline task %s failed", task.id)
            result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:2000], "summary": "", "attempts": []}
        if not self.store.accept_result(task.id, result):
            log.warning("inline task %s finished after it was closed (timed out?)", task.id)

    # -- tools -----------------------------------------------------------------------
    def tool_context(self, task: Task) -> tools.ToolContext:
        cfg = self.projects.for_project(task.project_path) or self.projects.defaults
        repo = (task.bundle or {}).get("repo") or {}
        ref = repo.get("sha") or task.target.get("sha") or "HEAD"
        return tools.ToolContext(self.project_gl(cfg), task.project_id, ref, self.settings.bot_username)

    def call_tool(self, task: Task, name: str, args: dict[str, Any]) -> str:
        """Run one tool call from a sandbox job. Raises PermissionError for tools the agent was not given."""
        agent = self.agent_for(task)
        if name not in agent.tools:
            raise PermissionError(f"agent {agent.name} may not use tool {name}")
        if not self.store.use_tool_call(task.id, TOOL_CALL_CAP):
            return f"error: the tool call budget ({TOOL_CALL_CAP}) is used up. Submit your answer now."
        log.info("task %s (%s) tool %s", task.id, agent.name, name)
        content = tools.run(name, self.tool_context(task), args)
        try:
            self.store.log_tool_call(task.id, name, args, content, max_entries=TOOL_CALL_CAP)
        except Exception:  # noqa: BLE001 - the log only feeds eval export
            log.exception("could not log tool call for task %s", task.id)
        return content

    # -- results & housekeeping ----------------------------------------------------------
    def apply_result(self, task: Task) -> None:
        agent = self.agent_for(task)
        cfg = self.projects.for_project(task.project_path) or self.projects.defaults
        ctx = OutputContext(self.project_gl(cfg), task, agent, task.result or {}, cfg, self.settings,
                            handoff=lambda target, instruction: self._handoff(task, cfg, target, instruction))
        try:
            outcome = outputs.apply(ctx)
            self.store.update(task.id, status=DONE, outcome=outcome[:500], produced=ctx.produced)
            try:
                self.store.add_links(task, feedback.links_for(ctx))
            except Exception:  # noqa: BLE001 - links only feed metrics
                log.exception("recording links for task %s failed", task.id)
            log.info("task %s (%s) done: %s", task.id, agent.name, outcome)
        except Exception as exc:  # noqa: BLE001
            log.exception("applying result failed for task %s", task.id)
            self.store.update(task.id, status=FAILED, error=str(exc)[:2000])
            self._notify_failure(task, f"The `{agent.name}` agent finished, but I could not apply its result: {str(exc)[:300]}")
        self._finished(task.id)

    def _finished(self, task_id: str) -> None:
        """A task reached done/failed: let its flow (if any) move on."""
        task = self.store.get(task_id)
        if task is None or not task.flow_run_id:
            return
        try:
            self.flows.on_task_finished(task)
        except Exception:  # noqa: BLE001
            log.exception("advancing the flow of task %s failed", task_id)

    def _handoff(self, task: Task, cfg: ProjectConfig, target: str, instruction: str) -> str:
        """Route a chat agent's handoff. Read-only agents start at once; anything that changes code, and flows,
        only get the command a human must post, so a person always confirms before code changes."""
        t, mention = task.target, self.settings.mention
        noteable = t.get("noteable_type")
        instruction = " ".join(instruction.split())[:300]
        flow = next((f for f in self.registry.flows_for(cfg.agents) if f.name == target), None)
        if flow is not None:
            command = next((c for tr in flow.triggers if tr.event == "note" and noteable in tr.targets for c in tr.commands), None)
            if command is None:
                return ""
            return f"The `{flow.name}` flow can do this. To start it, reply:\n\n```\n{mention} {command} {instruction}\n```"
        candidates = [a for n in cfg.agents if (a := self.registry.agents.get(n))]
        try:
            candidates += self.catalog.get(self.project_gl(cfg), task.project_id, cfg.custom_agents).agents
        except Exception:  # noqa: BLE001
            pass
        agent = next((a for a in candidates if a.name == target and a.output != "chat"), None)
        if agent is None:
            return ""
        command = next((c for tr in agent.triggers if tr.event == "note" and noteable in tr.targets for c in tr.commands), None)
        if command is None:
            return ""  # this agent does not work on this kind of object
        if agent.mode == "readonly":
            subject = f"{task.project_id}:{noteable}:{t.get('iid') or t.get('sha')}"
            if self.store.count_for_subject(agent.name, subject, ACTIVE):
                return f"The `{agent.name}` agent is already working on this."
            created = self.store.create(
                agent=agent.name, project_id=task.project_id, project_path=task.project_path,
                dedupe_key=f"{agent.name}:handoff:{task.id}", subject_key=subject, target=t, command=command,
                instruction=instruction, agent_json=agent.model_dump_json())
            return f"I've asked the `{agent.name}` agent to look into this; it will post here." if created else ""
        return f"The `{agent.name}` agent can make this change. To start it, reply:\n\n```\n{mention} {command} {instruction}\n```"

    def _notify_failure(self, task: Task, message: str) -> None:
        try:
            agent = self.agent_for(task)
            cfg = self.projects.for_project(task.project_path) or self.projects.defaults
            OutputContext(self.project_gl(cfg), task, agent, {}, cfg, self.settings).notify(message)
        except Exception:  # noqa: BLE001
            log.exception("could not post failure note for task %s", task.id)

    def check_dispatched(self) -> None:
        now = utcnow()
        for task in self.store.list_by_status(DISPATCHED):
            cfg = self.projects.for_project(task.project_path) or self.projects.defaults
            if task.deadline and now > task.deadline:
                self._fail_sandbox(task, cfg, "timed out", cancel=True)
                continue
            if task.sandbox_pipeline_id and now - task.updated_at > timedelta(seconds=60):
                try:
                    status = self.sandbox_gl(cfg).pipeline(self.settings.sandbox_project, task.sandbox_pipeline_id)["status"]
                except Exception:  # noqa: BLE001
                    continue
                if status in ("failed", "canceled", "skipped", "success"):
                    # A finished sandbox that never posted a result crashed before reporting.
                    fresh = self.store.get(task.id)
                    if fresh and fresh.status == DISPATCHED:
                        self._fail_sandbox(task, cfg, f"sandbox pipeline ended ({status}) without a result")
        # Recover tasks orphaned mid-dispatch by a worker crash.
        for task in self.store.list_by_status(DISPATCHING):
            if now - task.updated_at > timedelta(minutes=10):
                self.store.update(task.id, status=QUEUED)

    def _fail_sandbox(self, task: Task, cfg: ProjectConfig, reason: str, cancel: bool = False) -> None:
        if cancel and task.sandbox_pipeline_id:
            try:
                self.sandbox_gl(cfg).cancel_pipeline(self.settings.sandbox_project, task.sandbox_pipeline_id)
            except Exception:  # noqa: BLE001
                pass
        self.store.update(task.id, status=FAILED, error=reason)
        where = (f" Sandbox pipeline: {self.settings.gitlab_url}/{self.settings.sandbox_project}/-/pipelines/"
                 f"{task.sandbox_pipeline_id}" if task.sandbox_pipeline_id else "")
        self._notify_failure(task, f"The `{task.agent}` agent did not finish: {reason}.{where}")
        self._finished(task.id)

    def run_once(self) -> None:
        for task in self.store.claim_queued(max(self.settings.chat_workers, 1), self.inline_agents, in_lane=True):
            self.dispatch(task)
        for task in self.store.claim_queued(self.settings.max_concurrent_sandboxes, self.inline_agents):
            self.dispatch(task)
        for task in self.store.list_by_status(RESULT_RECEIVED):
            self.apply_result(task)
        self.check_dispatched()
        self.reactions.maybe_poll()
