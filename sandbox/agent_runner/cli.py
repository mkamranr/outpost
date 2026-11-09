"""agent-runner entrypoint.

Online (inside the sandbox CI job):
    agent-runner                      # reads AGENT_TASK_ID / AGENT_TASK_TICKET / AGENT_ORCHESTRATOR_URL / CI_JOB_TOKEN

Offline (evaluation and local development, no GitLab or orchestrator needed):
    agent-runner --bundle-file task.json --repo-dir ./checkout --result-file result.json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

from . import repo as git_repo
from .llm import LLMConfig
from .remote import RemoteTools, call
from .runner import Runner

log = logging.getLogger("agent_runner")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-runner")
    parser.add_argument("--bundle-file", type=Path)
    parser.add_argument("--repo-dir", type=Path, help="use an existing checkout instead of cloning")
    parser.add_argument("--result-file", type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)

    offline = args.bundle_file is not None
    if offline:
        bundle = json.loads(args.bundle_file.read_text(encoding="utf-8"))
        orch = ticket = None
    else:
        task_id = os.environ["AGENT_TASK_ID"]
        ticket = os.environ["AGENT_TASK_TICKET"]
        orch = os.environ["AGENT_ORCHESTRATOR_URL"].rstrip("/")
        bundle = call("GET", f"{orch}/api/tasks/{task_id}/bundle", ticket)
        assert bundle["task_id"] == task_id

    remote = None
    if bundle.get("tools"):
        remote = (RemoteTools.canned(bundle["tools"], bundle.get("tool_responses") or {}) if offline
                  else RemoteTools.http(bundle["tools"], orch, bundle["task_id"], ticket))

    agent = bundle["agent"]
    timeout_min = float(os.environ.get("AGENT_TIMEOUT_MINUTES") or agent.get("timeout_minutes", 30))
    deadline = time.time() + timeout_min * 60 - 60  # leave a minute to report
    log.info("task %s: agent %s (%s/%s) on %s@%s", bundle["task_id"], agent["name"], agent["mode"], agent["engine"],
             bundle["repo"]["path"], bundle["repo"]["sha"][:8])

    result: dict[str, Any]
    workdir = Path(tempfile.mkdtemp(prefix="agent-"))
    try:
        if args.repo_dir:
            repo = args.repo_dir.resolve()
        else:
            repo = workdir / "repo"
            merge = bundle["repo"].get("merge_target") or {}
            git_repo.clone(bundle["repo"]["http_url"], bundle["repo"]["sha"], repo, os.environ.get("CI_JOB_TOKEN"),
                           ref=bundle["repo"].get("ref"), merge_sha=merge.get("sha"), merge_ref=merge.get("ref"))
        result = Runner(bundle, repo, workdir, LLMConfig.from_env(), deadline, remote=remote).run()
    except Exception as exc:  # noqa: BLE001 - always report back, even on failure
        log.error("agent failed: %s", traceback.format_exc())
        result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:2000], "summary": "", "attempts": []}

    log.info("result: status=%s classification=%s changes=%d comments=%d", result.get("status"),
             result.get("classification"), len(result.get("changes") or []), len(result.get("comments") or []))
    if offline:
        if args.result_file:
            args.result_file.write_text(json.dumps(result, indent=2), encoding="utf-8")
        else:
            print(json.dumps(result, indent=2))
    else:
        call("POST", f"{orch}/api/tasks/{bundle['task_id']}/result", ticket, result)
    return 0 if result.get("status") != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
