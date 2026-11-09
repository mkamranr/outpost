"""`@agent rebase`: bring a merge request up to date with its target branch.

No conflicts: GitLab's own rebase does it, with no model involved (only on branches agents may push to).
Conflicts: a sandbox merges the target in, the model resolves the conflicts, the tests run, and the result is
opened as a replacement Draft MR cut from the target branch (the Commits API cannot write merge commits, and
agents never rewrite branches people own).
"""
from __future__ import annotations

import time
from typing import Any

from .config import ProjectConfig
from .gitlab_client import GitLab

REBASE_POLL_SECONDS = 2.0
REBASE_POLLS = 15
# GitLab computes mergeability in the background; until it is done, has_conflicts means nothing.
_STILL_CHECKING = {"checking", "unchecked", "preparing", "approvals_syncing"}


class Handled(str):
    """A build_bundle reason meaning the task was dealt with without a sandbox: post it as the answer."""


def prepare(gl: GitLab, cfg: ProjectConfig, target: dict[str, Any]) -> tuple[Handled | None, dict[str, str] | None]:
    """Either handle the request here (returns the message) or return the merge target for the sandbox."""
    pid, iid = target["project_id"], target["iid"]
    mr = gl.merge_request(pid, iid, include_diverged_commits_count="true")
    source, into = mr["source_branch"], mr["target_branch"]
    if mr.get("state", "opened") != "opened":
        return Handled(f"!{iid} is {mr.get('state')}; only open merge requests can be brought up to date."), None
    if mr.get("source_project_id") != mr.get("project_id"):
        return Handled("This merge request comes from a fork; I can only work with branches in this project."), None
    if mr.get("detailed_merge_status") in _STILL_CHECKING or mr.get("merge_status") in ("checking", "unchecked"):
        return Handled(f"GitLab is still checking whether !{iid} can be merged. Ask me again in a minute."), None

    if mr.get("has_conflicts"):
        head = gl.branch(pid, into)
        if not head:
            return Handled(f"The target branch `{into}` no longer exists."), None
        return None, {"ref": into, "sha": head["commit"]["id"]}

    behind = mr.get("diverged_commits_count") or 0
    if not behind:
        return Handled(f"!{iid} has no conflicts and is up to date with `{into}`: nothing to do."), None
    if not (source.startswith("agent/") or cfg.allow_push_to_mr_branch):
        return Handled(f"!{iid} has no conflicts; it is {behind} commit(s) behind `{into}`. Use GitLab's **Rebase** "
                       "button on the merge request: I don't rewrite branches people own."), None

    gl.rebase_merge_request(pid, iid)
    state: dict[str, Any] = {"rebase_in_progress": True}
    for _ in range(REBASE_POLLS):
        state = gl.merge_request(pid, iid, include_rebase_in_progress="true")
        if not state.get("rebase_in_progress"):
            break
        time.sleep(REBASE_POLL_SECONDS)
    if state.get("rebase_in_progress"):
        return Handled(f"I asked GitLab to rebase `{source}` onto `{into}`; it is still running."), None
    if state.get("merge_error"):
        return Handled(f"GitLab could not rebase `{source}` onto `{into}`: {state['merge_error']}"), None
    return Handled(f"Rebased `{source}` onto `{into}` ({behind} commit(s)). There were no conflicts, so no model "
                   "was involved."), None
