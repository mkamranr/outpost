#!/usr/bin/env python3
"""Offline evaluation: run agents against seeded-failure fixtures and score them. No GitLab needed, only vLLM.

Run inside a sandbox image (it has agent-runner, Jinja2, PyYAML and pydantic in /opt/agent):

  docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD:/w" -w /w \
      -e AGENT_LLM_BASE_URL=http://vllm.internal:8000/v1 -e AGENT_LLM_MODEL=qwen3.8-27b \
      registry.internal/agent-platform/sandbox-python:0.1.0 agent-python eval/run.py [--case py-off-by-one]

Each fixture is eval/fixtures/<case>/{case.yaml, repo/}. Results go to eval/out/report.{json,md}.
GitLab tool calls (job logs, MRs, ...) are answered from the case's `tool_responses`; inline agents (chat) need no repo/.
Re-run after any prompt, model, engine or vLLM change; compare the pass rate with the previous report.
Options: --case NAME (repeatable), --engine aider|mini-swe, --include-drafts, --out DIR.
Cases exported from real tasks (eval/export_task.py) start as `draft: true` and are skipped until completed.
Case format, `expect` checks and the export workflow: eval/README.md.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "orchestrator"))
sys.path.insert(0, str(ROOT / "sandbox"))

import yaml  # noqa: E402

from agent_runner import repo as git_repo  # noqa: E402
from agent_runner.llm import LLM, LLMConfig  # noqa: E402
from agent_runner.remote import RemoteTools  # noqa: E402
from agent_runner.runner import Runner  # noqa: E402
from app import inline  # noqa: E402
from app.registry import Registry  # noqa: E402
from app.tools import schemas  # noqa: E402


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def prepare_repo(src: Path, dest: Path) -> str:
    shutil.copytree(src, dest)
    git(dest, "init", "-q")
    git(dest, "add", "-A")
    git(dest, "-c", "user.name=eval", "-c", "user.email=eval@local", "commit", "-qm", "fixture")
    return git(dest, "rev-parse", "HEAD")


def build_bundle(registry: Registry, case: dict, name: str, sha: str) -> dict:
    agent = registry.get(case["agent"])
    protected = case.get("protected_paths", [".gitlab/agent.yml"])
    variables = {
        "context": case.get("context", {}),
        "target": case.get("target", {}),
        "instruction": case.get("instruction", ""),
        "repo": {"path": f"eval/{name}", "ref": "main", "sha": sha},
        "project": {"protected_paths": protected, "test": case.get("test", []), "setup": case.get("setup", [])},
        "agent": {"name": agent.name, "max_attempts": agent.max_attempts},
        "bot_username": "agent-bot",
        "catalog": case.get("catalog", []),
    }
    return {
        "task_id": f"eval-{name}",
        "agent": {"name": agent.name, "mode": agent.mode, "engine": case.get("engine", agent.engine),
                  "output": agent.output, "classify": agent.classify, "max_attempts": agent.max_attempts,
                  "timeout_minutes": agent.timeout_minutes, "allow_ci_changes": agent.allow_ci_changes},
        "prompts": {role: registry.render(agent, role, variables) for role in agent.prompts},
        "repo": {"path": f"eval/{name}", "http_url": "", "ref": "main", "sha": sha},
        "project": {"setup": case.get("setup", []), "test": case.get("test", []), "protected_paths": protected},
        "context": case.get("context", {}),
        "tools": schemas(agent.tools),
    }


def recording_remote(bundle: dict, case: dict, used: list[str]) -> RemoteTools:
    canned = RemoteTools.canned(bundle["tools"], case.get("tool_responses") or {})

    def invoke(name: str, args: dict) -> str:
        used.append(name)
        return canned.call(name, args)
    return RemoteTools(bundle["tools"], invoke)


def score(case: dict, result: dict, repo: Path) -> dict:
    expect = case.get("expect", {})
    checks: dict[str, bool] = {}
    if "classification" in expect:
        checks["classification"] = result.get("classification") == expect["classification"]
    if "changes" in expect:
        checks["changes"] = bool(result.get("changes")) == expect["changes"]
    if expect.get("tests_pass"):
        # Do not trust the runner's own verdict: re-run the tests on the final tree.
        ok = all(git_repo.run_shell(c, repo, timeout=600).ok for c in case.get("test", []))
        checks["tests_pass"] = ok and bool(result.get("changes"))
    if "body_contains" in expect:
        checks["body_contains"] = all(s.lower() in (result.get("body") or "").lower() for s in expect["body_contains"])
    if "comment_on" in expect:
        checks["comment_on"] = any(c.get("path") == expect["comment_on"] for c in result.get("comments") or [])
    if "handoff" in expect:
        checks["handoff"] = (result.get("handoff") or {}).get("target") == expect["handoff"]
    triage = result.get("triage") or {}
    if "labels" in expect:  # issue triage: these labels must be suggested
        checks["labels"] = set(expect["labels"]) <= set(triage.get("labels") or [])
    if "duplicate_of" in expect:
        checks["duplicate_of"] = any(d.get("iid") == expect["duplicate_of"] for d in triage.get("duplicates") or [])
    if "tools_used" in expect:
        checks["tools_used"] = set(expect["tools_used"]) <= set(result.get("tools_used") or [])
    return checks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--case", action="append", help="run only these fixtures (repeatable)")
    ap.add_argument("--engine", help="override the edit engine (aider | mini-swe)")
    ap.add_argument("--out", type=Path, default=ROOT / "eval" / "out")
    ap.add_argument("--include-drafts", action="store_true", help="also run cases marked draft: true")
    args = ap.parse_args()

    registry = Registry.load(ROOT / "agents")
    llm = LLMConfig.from_env()
    fixtures = sorted(p for p in (ROOT / "eval" / "fixtures").iterdir() if (p / "case.yaml").is_file())
    if args.case:
        fixtures = [f for f in fixtures if f.name in args.case]
    shutil.rmtree(args.out, ignore_errors=True)
    args.out.mkdir(parents=True)

    rows, drafts = [], []
    for fixture in fixtures:
        case = yaml.safe_load((fixture / "case.yaml").read_text(encoding="utf-8"))
        if case.get("draft") and not args.include_drafts:
            drafts.append(fixture.name)
            print(f"== {fixture.name}: draft, skipped (complete `expect` and remove `draft: true`)", flush=True)
            continue
        if args.engine:
            case["engine"] = args.engine
        work = args.out / fixture.name
        work.mkdir()
        repo = work / "repo"
        sha = prepare_repo(fixture / "repo", repo) if (fixture / "repo").is_dir() else "0" * 40
        bundle = build_bundle(registry, case, fixture.name, sha)
        (work / "bundle.json").write_text(json.dumps(bundle, indent=2), encoding="utf-8")
        print(f"== {fixture.name} ({case['agent']}): {case.get('description', '')}", flush=True)
        started = time.time()
        used: list[str] = []
        remote = recording_remote(bundle, case, used)
        try:
            deadline = started + bundle["agent"]["timeout_minutes"] * 60
            if bundle["agent"]["engine"] == "inline":
                result = inline.run(LLM(llm), bundle, remote.call)
            else:
                result = Runner(bundle, repo, work, llm, deadline, remote=remote).run()
        except Exception as exc:  # noqa: BLE001
            result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        result["tools_used"] = used
        elapsed = time.time() - started
        (work / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        checks = score(case, result, repo)
        passed = bool(checks) and all(checks.values())
        rows.append({"case": fixture.name, "agent": case["agent"], "passed": passed, "checks": checks,
                     "status": result.get("status"), "classification": result.get("classification"),
                     "attempts": len(result.get("attempts") or []), "seconds": round(elapsed, 1),
                     "error": result.get("error")})
        print(f"   {'PASS' if passed else 'FAIL'} {checks} in {elapsed:.0f}s", flush=True)

    total = len(rows)
    passed = sum(r["passed"] for r in rows)
    report = {"model": llm.model, "engine_override": args.engine, "passed": passed, "total": total, "cases": rows,
              "skipped_drafts": drafts}
    (args.out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = [f"# Agent eval: {passed}/{total} passed (model `{llm.model}`)", "",
             "| Case | Agent | Result | Checks | Attempts | Time |", "| --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        checks = ", ".join(f"{k} {'✓' if v else '✗'}" for k, v in r["checks"].items())
        lines.append(f"| {r['case']} | {r['agent']} | {'PASS' if r['passed'] else 'FAIL'} | {checks} | "
                     f"{r['attempts']} | {r['seconds']}s |")
    if drafts:
        lines += ["", f"Skipped drafts: {', '.join(drafts)}"]
    (args.out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
