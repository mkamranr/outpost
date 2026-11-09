#!/usr/bin/env python3
"""Idempotent GitLab setup for the agent platform (stdlib only; run from any host that reaches GitLab).

Creates/updates:
  * the sandbox project (private, shared runners off, CI file, LLM variables, pipeline-variable permission)
  * on each target project: the webhook, the CI job-token allowlist entry for the sandbox, the agent labels,
    and Developer membership for the bot

Needs a token with Maintainer (or Owner) on the sandbox group and the target projects, used for setup only.

  python3 bootstrap.py --gitlab-url https://gitlab.internal --token-file ~/setup_token \
      --sandbox-project agent-platform/sandbox --orchestrator-url http://agent-orchestrator.internal:8080 \
      --webhook-secret-file ../secrets/webhook_secret --bot-username agent-bot \
      --llm-base-url http://vllm.internal:8000/v1 --llm-model qwen3.8-27b \
      --project platform/payments-api --project platform/web-frontend
"""
from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlencode

ROOT = Path(__file__).resolve().parents[2]
SANDBOX_CI = ROOT / "sandbox" / "sandbox-project" / ".gitlab-ci.yml"
LABELS = {
    "agent::plan": ("#6699cc", "Ask the planner agent for an implementation plan"),
    "agent::implement": ("#428bca", "Ask the code-writer agent to implement the approved plan"),
    "agent::review": ("#5cb85c", "Have the code-reviewer agent review this MR on every push"),
    "agent::generated": ("#8e44ad", "Opened by an agent"),
    "agent::security-fix": ("#d9534f", "Security fix opened by the security-remediator agent"),
    "agent::approved": ("#2e8b57", "Approve the waiting step of a running agent flow"),
    "agent::flow::issue-to-mr": ("#1f75cb", "Run the issue-to-mr flow: plan, approve, implement, review"),
    "agent::flow::deep-review": ("#1f75cb", "Run the mr-deep-review flow: review + security, then tests"),
}
DEVELOPER = 30


class GitLab:
    def __init__(self, url: str, token: str, ca_file: str | None, dry_run: bool):
        self.base = url.rstrip("/") + "/api/v4"
        self.token = token
        self.ctx = ssl.create_default_context(cafile=ca_file) if ca_file else None
        self.dry_run = dry_run

    def call(self, method: str, path: str, body: dict | None = None, ok404: bool = False, raw: bool = False):
        if self.dry_run and method != "GET":
            print(f"  [dry-run] {method} {path} {json.dumps(body)[:200] if body else ''}")
            return {}
        req = urllib.request.Request(self.base + path, method=method, headers={
            "PRIVATE-TOKEN": self.token, "Content-Type": "application/json"},
            data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=60) as resp:
                data = resp.read()
                if raw:
                    return data.decode("utf-8")
                return json.loads(data) if data else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and ok404:
                return None
            raise SystemExit(f"{method} {path} -> {exc.code}: {exc.read()[:400].decode(errors='replace')}")


def pid(path: str) -> str:
    return quote(path, safe="")


def ensure_sandbox(gl: GitLab, args) -> dict:
    print(f"sandbox project {args.sandbox_project}")
    project = gl.call("GET", f"/projects/{pid(args.sandbox_project)}", ok404=True)
    if project is None:
        namespace, _, name = args.sandbox_project.rpartition("/")
        group = gl.call("GET", f"/groups/{pid(namespace)}", ok404=True)
        if group is None:
            raise SystemExit(f"group {namespace} not found; create it first")
        project = gl.call("POST", "/projects", {
            "name": name, "path": name, "namespace_id": group["id"], "visibility": "private",
            "initialize_with_readme": True, "default_branch": "main",
            "description": "Runs agent sandbox jobs for the self-built agent platform. Do not use for anything else.",
        })
        print("  created")
    if gl.dry_run and not project:
        return {"id": 0, "default_branch": "main"}
    gl.call("PUT", f"/projects/{project['id']}", {
        "visibility": "private",
        "shared_runners_enabled": False,
        "ci_pipeline_variables_minimum_override_role": "developer",
        "issues_enabled": False, "wiki_enabled": False, "snippets_enabled": False,
    })
    content = SANDBOX_CI.read_text(encoding="utf-8")
    branch = project.get("default_branch") or "main"
    existing = gl.call("GET", f"/projects/{project['id']}/repository/files/{pid('.gitlab-ci.yml')}/raw?ref={branch}", ok404=True, raw=True)
    if existing != content:
        action = "update" if existing is not None else "create"
        gl.call("POST", f"/projects/{project['id']}/repository/commits", {
            "branch": branch, "commit_message": "Agent platform sandbox CI definition",
            "actions": [{"action": action, "file_path": ".gitlab-ci.yml", "content": content}]})
        print(f"  .gitlab-ci.yml {action}d")
    variables = {"AGENT_LLM_BASE_URL": (args.llm_base_url, False), "AGENT_LLM_MODEL": (args.llm_model, False)}
    if args.llm_api_key_file:
        variables["AGENT_LLM_API_KEY"] = (Path(args.llm_api_key_file).read_text().strip(), True)
    for key, (value, masked) in variables.items():
        if not value:
            continue
        body = {"key": key, "value": value, "masked": masked, "protected": False}
        if gl.call("GET", f"/projects/{project['id']}/variables/{key}", ok404=True) is None:
            gl.call("POST", f"/projects/{project['id']}/variables", body)
        else:
            gl.call("PUT", f"/projects/{project['id']}/variables/{key}", body)
        print(f"  CI variable {key} set")
    return project


def ensure_member(gl: GitLab, project_id: int, bot_id: int | None) -> None:
    if not bot_id:
        return
    member = gl.call("GET", f"/projects/{project_id}/members/all/{bot_id}", ok404=True)
    if member is None:
        gl.call("POST", f"/projects/{project_id}/members", {"user_id": bot_id, "access_level": DEVELOPER})
        print("  bot added as Developer")
    elif member.get("access_level", 0) < DEVELOPER:
        print(f"  WARNING: bot has access level {member['access_level']} (needs Developer = 30)")


def ensure_target(gl: GitLab, path: str, sandbox: dict, hook_url: str, secret: str, bot_id: int | None, verify_ssl: bool):
    print(f"target project {path}")
    project = gl.call("GET", f"/projects/{pid(path)}", ok404=True)
    if project is None:
        print("  NOT FOUND, skipped")
        return
    p = project["id"]
    hook = {
        "url": hook_url, "token": secret, "push_events": False, "pipeline_events": True, "note_events": True,
        "issues_events": True, "merge_requests_events": True, "job_events": False,
        "enable_ssl_verification": verify_ssl,
    }
    existing = next((h for h in gl.call("GET", f"/projects/{p}/hooks") or [] if h.get("url") == hook_url), None)
    if existing:
        gl.call("PUT", f"/projects/{p}/hooks/{existing['id']}", hook)
        print("  webhook updated")
    else:
        gl.call("POST", f"/projects/{p}/hooks", hook)
        print("  webhook created")
    # Let sandbox jobs clone this project with CI_JOB_TOKEN (read access only).
    allow = gl.call("GET", f"/projects/{p}/job_token_scope/allowlist") or []
    if not any(a.get("id") == sandbox.get("id") for a in allow):
        gl.call("POST", f"/projects/{p}/job_token_scope/allowlist", {"target_project_id": sandbox.get("id")})
        print("  sandbox added to CI job token allowlist")
    labels = {l["name"] for l in gl.call("GET", f"/projects/{p}/labels?per_page=100") or []}
    for name, (color, description) in LABELS.items():
        if name not in labels:
            gl.call("POST", f"/projects/{p}/labels", {"name": name, "color": color, "description": description})
            print(f"  label {name} created")
    ensure_member(gl, p, bot_id)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gitlab-url", required=True)
    ap.add_argument("--token-file", required=True, help="setup token (Maintainer/Owner); not the bot token")
    ap.add_argument("--sandbox-project", required=True)
    ap.add_argument("--orchestrator-url", required=True, help="URL GitLab uses to reach the orchestrator")
    ap.add_argument("--webhook-secret-file", required=True)
    ap.add_argument("--bot-username")
    ap.add_argument("--llm-base-url")
    ap.add_argument("--llm-model")
    ap.add_argument("--llm-api-key-file")
    ap.add_argument("--project", action="append", default=[], help="target project path (repeatable)")
    ap.add_argument("--ca-file")
    ap.add_argument("--no-webhook-ssl-verify", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    gl = GitLab(args.gitlab_url, Path(args.token_file).expanduser().read_text().strip(), args.ca_file, args.dry_run)
    bot_id = None
    if args.bot_username:
        users = gl.call("GET", "/users?" + urlencode({"username": args.bot_username})) or []
        if not users:
            raise SystemExit(f"bot user {args.bot_username} not found; create the service account first")
        bot_id = users[0]["id"]
    sandbox = ensure_sandbox(gl, args)
    ensure_member(gl, sandbox.get("id"), bot_id)
    secret = Path(args.webhook_secret_file).read_text().strip()
    for path in args.project:
        ensure_target(gl, path, sandbox, args.orchestrator_url.rstrip("/") + "/webhook", secret, bot_id,
                      not args.no_webhook_ssl_verify)
    print("\nNext steps:")
    print(" * Register the agent-sandbox runner on the sandbox project (see deploy/runner/config.toml.example).")
    print(" * Admin > Settings > Network > Outbound requests: allow webhooks to the orchestrator host.")
    print(" * Add each target project to deploy/config/projects.yaml and restart the orchestrator.")
    print(" * Protect main and require MR approval on each target project, so agents can never merge alone.")


if __name__ == "__main__":
    sys.exit(main())
