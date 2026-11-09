"""Turn a finished task into an offline eval fixture (eval/fixtures/<name>/{case.yaml, repo/}).

The case replays exactly what the agent saw: the collected context, the instruction, and the tool results it got.
Expectations cannot be inferred, so the case is exported as a draft for a human to complete.
"""
from __future__ import annotations

import io
import re
import tarfile
from collections import defaultdict
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

import yaml

from .tasks import Task

if TYPE_CHECKING:
    from .platform import Platform

HEADER = """\
# Exported from an Outpost task. Draft: eval/run.py skips it until you
#   1. describe what a good result is (description),
#   2. fill in `expect` (classification, changes, tests_pass, body_contains, comment_on, handoff, tools_used),
#   3. delete `draft: true`.
"""
_NAME = re.compile(r"[^a-z0-9-]+")


def fixture_name(task: Task, requested: str | None = None) -> str:
    name = _NAME.sub("-", (requested or f"{task.agent}-{task.id[:8]}").lower()).strip("-")
    return name[:60] or f"task-{task.id[:8]}"


def _draft_expect(task: Task, agent: Any) -> dict[str, Any]:
    result = task.result or {}
    if agent.output == "pipeline_fix":
        expect: dict[str, Any] = {"classification": result.get("classification") or "code"}
        if expect["classification"] != "flaky":
            expect["tests_pass"] = True
        return expect
    if agent.mode == "edit":
        return {"tests_pass": True}
    if agent.output == "review":
        return {"comment_on": ""}
    return {"body_contains": []}


def build_case(platform: "Platform", task: Task) -> dict[str, Any]:
    bundle = task.bundle
    if bundle is None:
        raise ValueError("the task never collected context (it was skipped before dispatch)")
    agent = platform.agent_for(task)
    project = bundle.get("project") or {}
    responses: dict[str, list[str]] = defaultdict(list)
    for call in task.tool_log:
        responses[call["name"]].append(call["result"])
    feedback = sorted({f.kind for f in platform.store.feedback_for([task.id])})
    case: dict[str, Any] = {
        "agent": agent.name,
        "description": f"TODO: what should the agent have done? (exported from task {task.id[:8]} on "
                       f"{task.project_path}; outcome: {task.outcome or task.error or task.status})",
        "draft": True,
        "source": {"task": task.id, "project": task.project_path, "status": task.status,
                   "outcome": task.outcome, "error": task.error, "feedback": feedback},
        "instruction": task.instruction,
        "target": task.target,
        "test": project.get("test", []),
        "setup": project.get("setup", []),
        "protected_paths": project.get("protected_paths", [".gitlab/agent.yml"]),
        "context": bundle.get("context") or {},
    }
    if agent.output == "chat":
        cfg = platform.projects.for_project(task.project_path) or platform.projects.defaults
        try:
            case["catalog"] = platform.enabled_catalog(task.project_id, cfg)
        except Exception:  # noqa: BLE001
            case["catalog"] = []
    if responses:
        case["tool_responses"] = {k: v[0] if len(v) == 1 else v for k, v in responses.items()}
    case["expect"] = _draft_expect(task, agent)
    return case


def _safe(path: str) -> str | None:
    p = PurePosixPath(path)
    if p.is_absolute() or ".." in p.parts or not p.parts or p.parts[0] == ".git" or "\\" in path:
        return None
    return str(p)


def _touched_paths(task: Task) -> list[str]:
    paths = []
    for change in (task.result or {}).get("changes") or []:
        if change.get("action") in ("update", "delete", "move"):
            paths.append(change.get("previous_path") or change.get("path"))
    for call in task.tool_log:
        if isinstance(call.get("args"), dict) and isinstance(call["args"].get("path"), str):
            paths.append(call["args"]["path"])
    return [p for p in dict.fromkeys(paths) if p and _safe(p)]


def _add(tar: tarfile.TarFile, path: str, data: bytes) -> None:
    info = tarfile.TarInfo(path)
    info.size = len(data)
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(data))


def export_archive(platform: "Platform", task: Task, name: str) -> tuple[bytes, dict[str, Any]]:
    """A tar.gz holding <name>/case.yaml and <name>/repo/. Returns (archive, info for the caller)."""
    case = build_case(platform, task)
    agent = platform.agent_for(task)
    repo = (task.bundle or {}).get("repo") or {}
    cfg = platform.projects.for_project(task.project_path) or platform.projects.defaults
    gl = platform.project_gl(cfg)
    files: dict[str, bytes] = {}
    repo_mode = "none"
    if agent.engine != "inline" and repo.get("sha"):
        snapshot = gl.archive(task.project_id, repo["sha"], platform.settings.fixture_max_repo_mb * 1024 * 1024)
        if snapshot is not None:
            repo_mode = "full"
            with tarfile.open(fileobj=io.BytesIO(snapshot), mode="r:gz") as src:
                for member in src.getmembers():
                    if not member.isfile():
                        continue  # no links or devices from an archive
                    inner = member.name.split("/", 1)[1] if "/" in member.name else ""
                    safe = _safe(inner) if inner else None
                    fh = src.extractfile(member) if safe else None
                    if fh is not None:
                        files[safe] = fh.read()
        else:
            repo_mode = "partial"
            for path in _touched_paths(task):
                content = gl.raw_file(task.project_id, path, repo["sha"])
                if content is not None:
                    files[path] = content.encode("utf-8")
            if files:
                case["description"] += " Repository too large: only the files the task touched were exported."
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        text = HEADER + yaml.safe_dump(case, sort_keys=False, allow_unicode=True, width=120)
        _add(tar, f"{name}/case.yaml", text.encode("utf-8"))
        for path, data in sorted(files.items()):
            _add(tar, f"{name}/repo/{path}", data)
    return out.getvalue(), {"name": name, "repo": repo_mode, "files": len(files)}
