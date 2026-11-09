"""Read-only tool-calling loop used by agents that analyse but never edit (planner, reviewer, security analyst, chat).

Two tool sources: file tools over a local checkout (when there is one), and GitLab tools executed by the
orchestrator (`RemoteTools`). Edit agents also use this loop for a short investigation before editing.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from .llm import LLM, extract_json
from .remote import RemoteTools

MAX_TOOL_CHARS = 12000
MAX_TRANSCRIPT_CHARS = 140000  # ~40K tokens: stop exploring and force a submit beyond this

SUBMIT_SCHEMAS: dict[str, dict[str, Any]] = {
    "note": {
        "type": "object",
        "properties": {"body": {"type": "string", "description": "The full markdown comment to post"}},
        "required": ["body"],
    },
    "review": {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "comments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "line": {"type": "integer"},
                        "body": {"type": "string"},
                    },
                    "required": ["path", "line", "body"],
                },
            },
        },
        "required": ["summary", "comments"],
    },
    "brief": {
        "type": "object",
        "properties": {
            "findings": {"type": "string", "description": "What you learned that matters for making the change"},
            "suspect_files": {"type": "array", "items": {"type": "string"},
                              "description": "Repo-relative paths that most likely need editing"},
        },
        "required": ["findings"],
    },
    "chat": {
        "type": "object",
        "properties": {
            "body": {"type": "string", "description": "The full markdown reply to post"},
            "handoff": {
                "type": "object",
                "description": "Optional: hand the request to a specialist agent or flow from the catalog",
                "properties": {"target": {"type": "string"}, "instruction": {"type": "string"}},
                "required": ["target"],
            },
        },
        "required": ["body"],
    },
    "triage": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "One or two sentences: what the issue is about"},
            "labels": {"type": "array", "items": {"type": "string"},
                       "description": "Up to 5 existing project labels that fit"},
            "duplicates": {
                "type": "array",
                "description": "Existing issues this one likely duplicates (only ones you looked at)",
                "items": {"type": "object", "properties": {"iid": {"type": "integer"}, "reason": {"type": "string"}},
                          "required": ["iid", "reason"]},
            },
            "missing_info": {"type": "array", "items": {"type": "string"},
                             "description": "Questions for the reporter about missing details (versions, steps...)"},
        },
        "required": ["summary", "labels", "duplicates", "missing_info"],
    },
}

SYSTEM = (
    "You are a careful software engineering agent working in a read-only checkout of a git repository. "
    "Use the tools to inspect files before drawing conclusions. You cannot modify files or access the network. "
    "When you have enough information, call the `submit` tool exactly once with your final answer."
)
GITLAB_TOOLS_HINT = (
    " You also have read-only GitLab tools for this project (issues, merge requests, pipelines, job logs). "
    "Use them when the task refers to things outside the checkout."
)
LOCAL_TOOLS = ("list_files", "read_file", "grep")


def _fn(schema: dict[str, Any]) -> dict[str, Any]:
    return {"type": "function", "function": schema}


def _tools(output: str, remote: RemoteTools | None = None, local: bool = True) -> list[dict[str, Any]]:
    def fn(name: str, description: str, params: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return _fn({"name": name, "description": description,
                    "parameters": {"type": "object", "properties": params, "required": required}})

    tools = []
    if local:
        tools += [
            fn("list_files", "List tracked files under a directory (repo-relative).",
               {"path": {"type": "string", "default": "."}}, []),
            fn("read_file", "Read a text file, optionally a line range (1-based, inclusive).",
               {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, ["path"]),
            fn("grep", "Search tracked files with an extended regular expression. Returns file:line:text matches.",
               {"pattern": {"type": "string"}, "path": {"type": "string", "default": "."}}, ["pattern"]),
        ]
    if remote:
        tools += [_fn(s) for s in remote.schemas if s["name"] not in LOCAL_TOOLS and s["name"] != "submit"]
    tools.append(_fn({"name": "submit", "description": "Submit the final answer.", "parameters": SUBMIT_SCHEMAS[output]}))
    return tools


class Tools:
    def __init__(self, repo: Path):
        self.repo = repo.resolve()

    def _resolve(self, path: str) -> Path:
        p = (self.repo / (path or ".")).resolve()
        if p != self.repo and self.repo not in p.parents:
            raise ValueError("path outside repository")
        if ".git" in p.relative_to(self.repo).parts:
            raise ValueError("the .git directory is not accessible")
        return p

    def _git(self, *args: str) -> str:
        proc = subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True, errors="replace", timeout=60)
        if proc.returncode > 1:  # 1 means "no matches" for git grep
            raise RuntimeError(proc.stderr.strip()[:300] or f"git exited {proc.returncode}")
        return proc.stdout

    def list_files(self, path: str = ".") -> str:
        target = self._resolve(path)
        files = self._git("ls-files", "--", str(target.relative_to(self.repo)) or ".").splitlines()
        more = f"\n[... {len(files) - 400} more ...]" if len(files) > 400 else ""
        return "\n".join(files[:400]) + more

    def read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        target = self._resolve(path)
        if not target.is_file():
            return f"error: {path} is not a file"
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, int(start_line or 1))
        end = min(len(lines), int(end_line or start + 399))
        body = "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))
        return body + (f"\n[file has {len(lines)} lines]" if end < len(lines) else "")

    def grep(self, pattern: str, path: str = ".") -> str:
        target = self._resolve(path)
        out = self._git("grep", "-n", "-I", "-E", "-e", pattern, "--", str(target.relative_to(self.repo)) or ".")
        lines = out.splitlines()
        return "\n".join(lines[:80]) + (f"\n[... {len(lines) - 80} more matches ...]" if len(lines) > 80 else "") or "no matches"

    def call(self, name: str, args: dict[str, Any]) -> str:
        if name not in ("list_files", "read_file", "grep"):
            return f"error: unknown tool {name}"
        try:
            result = getattr(self, name)(**args)
        except Exception as exc:  # noqa: BLE001 - report tool errors back to the model
            result = f"error: {exc}"
        return result if len(result) <= MAX_TOOL_CHARS else result[:MAX_TOOL_CHARS] + "\n[... truncated ...]"


def _valid(output: str, args: dict[str, Any] | None) -> bool:
    if not isinstance(args, dict):
        return False
    if output in ("note", "chat"):
        return isinstance(args.get("body"), str) and bool(args["body"].strip())
    if output == "brief":
        return isinstance(args.get("findings"), str)
    if output == "triage":
        return isinstance(args.get("summary"), str) and all(
            isinstance(args.get(k, []), list) for k in ("labels", "duplicates", "missing_info"))
    return isinstance(args.get("summary"), str) and isinstance(args.get("comments", []), list)


def explore(llm: LLM, repo: Path | None, prompt: str, output: str, deadline: float, max_turns: int = 30,
            remote: RemoteTools | None = None, system: str | None = None) -> dict[str, Any]:
    """Run the tool loop until the model submits. Returns the submit arguments.

    `repo=None` runs without a checkout (only the remote tools), as the in-orchestrator chat agent does.
    """
    tools = Tools(repo) if repo is not None else None
    definitions = _tools(output, remote, local=tools is not None)
    system = system or (SYSTEM + (GITLAB_TOOLS_HINT if remote else ""))
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    transcript = len(prompt)
    for turn in range(max_turns):
        force = transcript > MAX_TRANSCRIPT_CHARS or turn == max_turns - 2 or time.time() > deadline - 120
        if force:
            messages.append({"role": "user", "content": "Stop exploring now and call `submit` with your final answer."})
        reply = llm.chat(messages, tools=definitions)
        if not reply.tool_calls:
            # Some models answer in plain text instead of calling submit; accept valid JSON.
            parsed = extract_json(reply.content)
            if _valid(output, parsed):
                return parsed
            if output in ("note", "chat") and reply.content and force:
                return {"body": reply.content}
            messages.append({"role": "assistant", "content": reply.content})
            messages.append({"role": "user", "content": "Use the tools, then call `submit` with the final answer."})
            continue
        messages.append({"role": "assistant", "content": reply.content or None, "tool_calls": reply.tool_calls})
        for call in reply.tool_calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = None
            if name == "submit":
                if _valid(output, args):
                    return args
                content = "error: submit arguments did not match the schema; try again"
            elif args is None:
                content = "error: arguments were not valid JSON"
            elif tools is not None and name in LOCAL_TOOLS:
                content = tools.call(name, args)
            elif remote is not None:
                content = remote.call(name, args)
                if len(content) > MAX_TOOL_CHARS:
                    content = content[:MAX_TOOL_CHARS] + "\n[... truncated ...]"
            else:
                content = f"error: unknown tool {name}"
            transcript += len(content)
            messages.append({"role": "tool", "tool_call_id": call.get("id", name), "content": content})
    raise RuntimeError("the model did not submit an answer within the turn limit")
