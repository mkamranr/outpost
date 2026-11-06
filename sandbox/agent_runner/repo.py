"""Git and shell helpers for the sandbox workspace."""
from __future__ import annotations

import base64
import fnmatch
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit


@dataclass
class CommandResult:
    command: str
    exit_code: int
    output: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, errors="replace")
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:1000]}")
    return proc.stdout


def run_shell(command: str, cwd: Path, timeout: float, env: dict[str, str] | None = None) -> CommandResult:
    try:
        proc = subprocess.run(
            ["bash", "-lc", command], cwd=cwd, capture_output=True, text=True, errors="replace",
            timeout=max(timeout, 1), env={**os.environ, **(env or {})},
        )
        return CommandResult(command, proc.returncode, (proc.stdout + proc.stderr))
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"").decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return CommandResult(command, -1, out + f"\n[timed out after {timeout:.0f}s]", timed_out=True)


def authenticated_url(http_url: str, job_token: str) -> str:
    parts = urlsplit(http_url)
    netloc = f"gitlab-ci-token:{quote(job_token, safe='')}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def clone(http_url: str, sha: str, dest: Path, job_token: str | None, ref: str | None = None) -> None:
    url = authenticated_url(http_url, job_token) if job_token else http_url
    dest.mkdir(parents=True, exist_ok=True)
    git(dest, "init", "-q")
    git(dest, "remote", "add", "origin", url)
    try:
        git(dest, "fetch", "-q", "--depth", "50", "origin", sha)
    except RuntimeError:
        if not ref:
            raise
        # Servers that refuse fetching by SHA: fetch the branch and find the commit in its history.
        git(dest, "fetch", "-q", "--depth", "200", "origin", f"refs/heads/{ref}")
    git(dest, "checkout", "-q", "--detach", sha)
    git(dest, "config", "user.name", "agent")
    git(dest, "config", "user.email", "agent@sandbox.local")
    # Drop the credential from the stored remote so tools in the sandbox never see it.
    git(dest, "remote", "set-url", "origin", http_url)


def exclude(repo: Path, patterns: list[str]) -> None:
    """Hide paths from git status/diff without touching the repo's .gitignore."""
    info = repo / ".git" / "info"
    info.mkdir(parents=True, exist_ok=True)
    with open(info / "exclude", "a", encoding="utf-8") as fh:
        for p in patterns:
            fh.write(p + "\n")


def untracked(repo: Path) -> set[str]:
    out = git(repo, "status", "--porcelain", "--untracked-files=normal", "-z")
    return {entry[3:] for entry in out.split("\0") if entry.startswith("?? ")}


def exclude_new_untracked(repo: Path, before: set[str]) -> list[str]:
    """Treat untracked files created by setup/test commands (caches, build output) as noise, not changes."""
    new = sorted(untracked(repo) - before)
    if new:
        exclude(repo, ["/" + p for p in new])
    return new


def has_changes(repo: Path) -> bool:
    return bool(git(repo, "status", "--porcelain").strip())


def diff_text(repo: Path, base: str, max_chars: int = 60000) -> str:
    git(repo, "add", "-A")
    text = git(repo, "diff", "--cached", base)
    git(repo, "reset", "-q")
    return text if len(text) <= max_chars else text[:max_chars] + "\n[... diff truncated ...]"


def _protected(path: str, patterns: list[str]) -> bool:
    return any(path.startswith(p) if p.endswith("/") else fnmatch.fnmatchcase(path, p) for p in patterns)


def collect_changes(repo: Path, base: str, protected: list[str]) -> tuple[list[dict], list[str]]:
    """Return Commits-API-shaped changes relative to `base`, plus paths dropped because they are protected."""
    git(repo, "add", "-A")
    out = git(repo, "diff", "--cached", "--name-status", "-M", "-z", base)
    git(repo, "reset", "-q")
    fields = [f for f in out.split("\0") if f]
    changes: list[dict] = []
    dropped: list[str] = []
    i = 0
    while i < len(fields):
        status = fields[i]
        code = status[0]
        if code == "R":
            old, new = fields[i + 1], fields[i + 2]
            i += 3
            paths, entry = [old, new], {"action": "move", "previous_path": old, "path": new}
        else:
            path = fields[i + 1]
            i += 2
            action = {"A": "create", "D": "delete"}.get(code, "update")
            paths, entry = [path], {"action": action, "path": path}
        if any(_protected(p, protected) for p in paths):
            dropped.extend(paths)
            continue
        if entry["action"] != "delete":
            file = repo / entry["path"]
            if file.is_symlink() or not file.is_file():
                dropped.append(entry["path"])
                continue
            entry["content_b64"] = base64.b64encode(file.read_bytes()).decode()
        changes.append(entry)
    return changes, dropped


def existing_paths(repo: Path, candidates: list[str], limit: int = 8) -> list[str]:
    out = []
    for c in candidates:
        c = c.strip().removeprefix("./")
        if c and (repo / c).is_file() and c not in out:
            out.append(c)
        if len(out) >= limit:
            break
    return out
