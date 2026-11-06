"""Validate file changes returned by a sandbox and convert them to Commits API actions.

The sandbox is untrusted (it ran a model over untrusted input), so the orchestrator re-checks every
path before anything is committed with its write token.
"""
from __future__ import annotations

import base64
import fnmatch
import posixpath
from typing import Any

MAX_FILES = 50
MAX_FILE_BYTES = 1_000_000
CI_PATTERNS = (".gitlab-ci.yml", "*.gitlab-ci.yml", ".gitlab/ci/")


class ChangeRejected(ValueError):
    pass


def _safe_path(path: str) -> str:
    if not path or path.startswith("/") or "\\" in path or "\x00" in path:
        raise ChangeRejected(f"invalid path {path!r}")
    norm = posixpath.normpath(path)
    if norm.startswith("..") or norm == "." or norm.startswith(".git/") or norm == ".git":
        raise ChangeRejected(f"path escapes repository: {path!r}")
    return norm


def _matches(path: str, patterns: list[str] | tuple[str, ...]) -> bool:
    """fnmatch semantics (`*` also crosses `/`); a pattern ending in `/` protects a whole directory."""
    return any(path.startswith(p) if p.endswith("/") else fnmatch.fnmatchcase(path, p) for p in patterns)


def is_ci_path(path: str) -> bool:
    return _matches(path, CI_PATTERNS)


def to_actions(changes: list[dict[str, Any]], protected_paths: list[str], allow_ci: bool) -> list[dict[str, Any]]:
    if not changes:
        return []
    if len(changes) > MAX_FILES:
        raise ChangeRejected(f"{len(changes)} files changed; limit is {MAX_FILES}")
    actions = []
    for change in changes:
        action = change.get("action")
        if action not in ("create", "update", "delete", "move"):
            raise ChangeRejected(f"unsupported action {action!r}")
        path = _safe_path(str(change.get("path", "")))
        paths = [path]
        entry: dict[str, Any] = {"action": action, "file_path": path}
        if action == "move":
            prev = _safe_path(str(change.get("previous_path", "")))
            entry["previous_path"] = prev
            paths.append(prev)
        for p in paths:
            if _matches(p, protected_paths):
                raise ChangeRejected(f"{p} is a protected path")
            if is_ci_path(p) and not allow_ci:
                raise ChangeRejected(f"{p} is CI configuration, which this agent may not change")
        if action in ("create", "update", "move") and change.get("content_b64") is not None:
            content = change["content_b64"]
            try:
                size = len(base64.b64decode(content, validate=True))
            except ValueError as exc:
                raise ChangeRejected(f"{path}: content is not valid base64") from exc
            if size > MAX_FILE_BYTES:
                raise ChangeRejected(f"{path}: {size} bytes exceeds {MAX_FILE_BYTES}")
            entry["content"] = content
            entry["encoding"] = "base64"
        elif action in ("create", "update"):
            raise ChangeRejected(f"{path}: missing content")
        actions.append(entry)
    return actions


def ci_contents(actions: list[dict[str, Any]]) -> dict[str, str]:
    """Decoded new contents of CI files touched by the change, for linting."""
    out = {}
    for a in actions:
        if is_ci_path(a["file_path"]) and a.get("content") is not None:
            out[a["file_path"]] = base64.b64decode(a["content"]).decode("utf-8", errors="replace")
    return out
