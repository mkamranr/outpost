"""Execute one task bundle inside the sandbox and produce a result for the orchestrator."""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any

from . import repo as git_repo
from .engines import get_engine
from .explore import explore
from .llm import LLM, LLMConfig, extract_json
from .remote import RemoteTools

log = logging.getLogger("agent_runner")

TAIL_CHARS = 5000
MAX_MERGE_FILES = 50  # the orchestrator commits at most this many files
_PATHISH = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]{1,8})(?::\d+)?")


def _tail(text: str, n: int = TAIL_CHARS) -> str:
    return text if len(text) <= n else "[...]\n" + text[-n:]


def candidate_files(bundle: dict[str, Any], repo: Path, hints: list[str]) -> list[str]:
    """Files worth adding to the engine's context: model hints first, then paths seen in traces and diffs."""
    seen: list[str] = list(hints)
    ctx = bundle.get("context") or {}
    texts: list[str] = []
    for job in ctx.get("failed_jobs") or []:
        if isinstance(job, dict):
            texts.append(job.get("trace", ""))
    for key in ("pipeline_diff", "mr_diff"):
        if isinstance(ctx.get(key), dict):
            texts.append(ctx[key].get("diff", ""))
    pos = (ctx.get("discussion") or {}).get("position") if isinstance(ctx.get("discussion"), dict) else None
    if pos and pos.get("path"):
        seen.insert(0, pos["path"])
    for text in texts:
        seen += [m.group(1) for m in _PATHISH.finditer(text or "")]
    return git_repo.existing_paths(repo, seen)


class Runner:
    def __init__(self, bundle: dict[str, Any], repo: Path, workdir: Path, llm_config: LLMConfig, deadline: float,
                 llm: LLM | None = None, engine: Any = None, remote: RemoteTools | None = None):
        self.bundle = bundle
        self.remote = remote
        self.agent = bundle["agent"]
        self.prompts = bundle["prompts"]
        self.project = bundle.get("project") or {}
        self.repo = repo
        self.workdir = workdir
        self.llm_config = llm_config
        self.llm = llm or LLM(llm_config)
        self.deadline = deadline
        self._engine = engine
        self.result: dict[str, Any] = {"status": "failed", "summary": "", "attempts": [], "changes": [], "comments": []}

    def remaining(self) -> float:
        return self.deadline - time.time()

    # -- phases ---------------------------------------------------------------------
    def setup(self) -> None:
        git_repo.exclude(self.repo, [".aider*"])
        before = git_repo.untracked(self.repo)
        for command in self.project.get("setup") or []:
            res = git_repo.run_shell(command, self.repo, timeout=min(self.remaining(), 900))
            if not res.ok:
                raise RuntimeError(f"setup command failed: {command}\n{_tail(res.output, 2000)}")
        git_repo.exclude_new_untracked(self.repo, before)

    def run_tests(self) -> tuple[bool | None, str]:
        commands = self.project.get("test") or []
        if not commands:
            return None, "no test command configured"
        before = git_repo.untracked(self.repo)
        outputs = []
        passed = True
        for command in commands:
            res = git_repo.run_shell(command, self.repo, timeout=max(self.remaining() - 60, 30))
            outputs.append(f"$ {command}\n{res.output}")
            if not res.ok:
                passed = False
                break
        git_repo.exclude_new_untracked(self.repo, before)
        return passed, _tail("\n".join(outputs))

    def classify(self) -> dict[str, Any]:
        reply = self.llm.chat([{"role": "user", "content": self.prompts["classify"]}], max_tokens=800)
        parsed = extract_json(reply.content) or {}
        cls = parsed.get("classification")
        if cls not in ("code", "ci_config", "flaky"):
            cls = "code"  # attempting a fix is safer than silently retrying
        return {"classification": cls, "reason": str(parsed.get("reason", ""))[:1000],
                "suspect_files": [str(f) for f in parsed.get("suspect_files") or []][:10]}

    def describe(self, diff: str) -> dict[str, str]:
        """Ask the model for an MR title and a human-readable summary of what it changed and why."""
        prompt = (
            "You just made the following change to fix the task described below. Write a short MR title "
            "(imperative, under 70 chars) and a markdown summary covering the root cause, what you changed, "
            "and anything a reviewer should double-check. Reply with only JSON: "
            '{"title": "...", "summary": "..."}\n\n'
            f"## Task\n{self.prompts['main'][:6000]}\n\n## Diff\n```diff\n{diff[:20000]}\n```"
        )
        try:
            parsed = extract_json(self.llm.chat([{"role": "user", "content": prompt}], max_tokens=1200).content) or {}
        except Exception:  # noqa: BLE001 - a missing summary must not lose a working fix
            parsed = {}
        return {"title": str(parsed.get("title") or "")[:200], "summary": str(parsed.get("summary") or "")[:8000]}

    def investigate(self) -> dict[str, Any]:
        """Before editing, let the model use the GitLab and file tools (which the edit engines cannot) to brief itself."""
        prompt = (
            "Before anyone edits code, investigate the task below. Use the file tools and the GitLab tools "
            "(issues, merge requests, pipelines, job logs) to find the root cause and the files that need to change. "
            "Do not propose a full patch. Call `submit` with your findings and the suspect files.\n\n"
            + self.prompts["main"]
        )
        budget = min(self.deadline, time.time() + max(self.remaining() * 0.25, 120))
        try:
            brief = explore(self.llm, self.repo, prompt, "brief", budget, max_turns=12, remote=self.remote)
        except Exception as exc:  # noqa: BLE001 - investigation is an aid; the engine can still work without it
            log.warning("investigation skipped: %s", exc)
            return {"findings": "", "suspect_files": []}
        return {"findings": str(brief.get("findings") or "")[:6000],
                "suspect_files": [str(f) for f in brief.get("suspect_files") or []][:10]}

    # -- modes ----------------------------------------------------------------------------
    def run_readonly(self) -> dict[str, Any]:
        answer = explore(self.llm, self.repo, self.prompts["main"], self.agent["output"], self.deadline, remote=self.remote)
        self.result.update(status="success")
        if self.agent["output"] == "review":
            self.result.update(summary=answer.get("summary", ""), comments=[
                c for c in answer.get("comments", []) if isinstance(c, dict) and {"path", "line", "body"} <= c.keys()
            ])
        else:
            self.result.update(body=answer["body"])
        return self.result

    def run_merge(self, merge: dict[str, str]) -> dict[str, Any]:
        """Merge the target branch in, have the engine resolve the conflicts, test, and return the tree vs the target."""
        base = merge["sha"]
        conflicted = git_repo.start_merge(self.repo, base)
        touched = git_repo.changed_paths(self.repo, base)
        self.result.update(conflicts=conflicted)
        protected = self.project.get("protected_paths") or []
        blocked = [p for p in touched if git_repo._protected(p, protected)]
        if len(touched) > MAX_MERGE_FILES or blocked:
            why = (f"the merge request changes {len(touched)} files compared with `{merge['ref']}` (at most "
                   f"{MAX_MERGE_FILES} can be committed)" if not blocked else
                   "it changes protected paths, which a replacement MR could not carry: "
                   + ", ".join(f"`{p}`" for p in blocked[:10]))
            self.result.update(status="failed", summary=f"I can't open a replacement MR: {why}. Resolve the "
                                                        "conflicts by hand or with GitLab's conflict editor.")
            return self.result

        engine = self._engine or get_engine(self.agent["engine"], self.llm_config, self.workdir)
        task = self.prompts["main"] + "\n\n## Files with conflicts\n" + "\n".join(f"- {p}" for p in conflicted)
        instruction: str | None = task if conflicted else None
        files = conflicted or touched[:20]
        passed: bool | None = False
        left: list[str] = list(conflicted)
        for n in range(1, int(self.agent.get("max_attempts", 3)) + 1):
            if self.remaining() < 120:
                self.result["attempts"].append({"n": n, "tests_passed": None, "note": "out of time"})
                break
            if instruction is not None:
                run = engine.run(self.repo, instruction, files, n, timeout=self.remaining() - 90)
                left = git_repo.conflict_markers(self.repo, conflicted)
                if left:
                    self.result["attempts"].append({"n": n, "tests_passed": None, "test_output_tail": _tail(run.log_tail, 1500),
                                                    "note": "conflict markers left in " + ", ".join(left)})
                    instruction = ("These files still contain conflict markers (`<<<<<<<` / `>>>>>>>` lines): "
                                   + ", ".join(left) + ". Resolve those conflicts now.\n\n" + task)
                    continue
            passed, output = self.run_tests()
            self.result["attempts"].append({"n": n, "tests_passed": passed, "test_output_tail": output,
                                            "note": "tests passed" if passed else ("no tests configured" if passed is None else "tests failed")})
            if passed is not False:
                break
            instruction = (
                "The conflicts are resolved but the project's tests fail. Here is the end of the test output:\n\n"
                f"```\n{_tail(output, 4000)}\n```\n\nFix the merged code. Do not weaken or delete tests."
            )

        changes, dropped = git_repo.collect_changes(self.repo, base, protected)
        self.result.update(changes=changes, diff=git_repo.diff_text(self.repo, base), tests_passed=passed)
        summary = (f"Merged `{merge['ref']}` ({base[:8]}) into the merge request and resolved conflicts in "
                   f"{len(conflicted)} file(s)." if conflicted else
                   f"Merged `{merge['ref']}` ({base[:8]}) into the merge request; git reported no conflicts here.")
        if dropped:
            summary += "\n\n_Changes to protected paths were discarded: " + ", ".join(f"`{p}`" for p in dropped) + "_"
        if passed is None:
            summary += "\n\n_No test command is configured for this project; rely on the MR pipeline._"
        ok = not left and passed is not False and bool(changes)
        if left:
            summary += "\n\nConflict markers are still left in: " + ", ".join(f"`{p}`" for p in left)
        self.result.update(status="success" if ok else "failed", summary=summary)
        return self.result

    def run_edit(self) -> dict[str, Any]:
        merge = self.bundle["repo"].get("merge_target")
        if merge:
            return self.run_merge(merge)
        base = self.bundle["repo"]["sha"]
        hints: list[str] = []
        if self.agent.get("classify"):
            c = self.classify()
            self.result.update(classification=c["classification"])
            if c["classification"] == "flaky":
                self.result.update(status="success", summary=c["reason"])
                return self.result
            hints = c["suspect_files"]
            self.result["summary"] = c["reason"]

        findings = ""
        if self.remote is not None:
            brief = self.investigate()
            hints = brief["suspect_files"] + [h for h in hints if h not in brief["suspect_files"]]
            findings = brief["findings"]

        engine = self._engine or get_engine(self.agent["engine"], self.llm_config, self.workdir)
        files = candidate_files(self.bundle, self.repo, hints)
        task = self.prompts["main"]
        if findings:
            task += f"\n\n## Findings from a prior investigation (verify before relying on them)\n{findings}"
        instruction = task
        passed: bool | None = False
        for n in range(1, int(self.agent.get("max_attempts", 3)) + 1):
            if self.remaining() < 120:
                self.result["attempts"].append({"n": n, "tests_passed": None, "note": "out of time"})
                break
            run = engine.run(self.repo, instruction, files, n, timeout=self.remaining() - 90)
            if not git_repo.has_changes(self.repo):
                self.result["attempts"].append({"n": n, "tests_passed": None, "note": "engine made no changes",
                                                "test_output_tail": _tail(run.log_tail, 1500)})
                instruction = ("You have not changed any files yet. Make the code change now by editing the files "
                               "directly.\n\n" + task)
                continue
            passed, output = self.run_tests()
            self.result["attempts"].append({"n": n, "tests_passed": passed, "test_output_tail": output,
                                            "note": "tests passed" if passed else ("no tests configured" if passed is None else "tests failed")})
            if passed is not False:
                break
            instruction = (
                "Your change is in place but the project's tests still fail. Here is the end of the test output:\n\n"
                f"```\n{_tail(output, 4000)}\n```\n\nFind and fix the remaining problem. Do not weaken or delete tests."
            )

        changes, dropped = git_repo.collect_changes(self.repo, base, self.project.get("protected_paths") or [])
        diff = git_repo.diff_text(self.repo, base)
        self.result.update(changes=changes, diff=diff, tests_passed=passed)
        if not changes:
            self.result.update(status="no_change")
            self.result["summary"] = (self.result["summary"] + "\n\nNo usable change was produced.").strip()
            return self.result
        described = self.describe(diff)
        reason = self.result.get("summary") or ""
        summary = described["summary"] or reason
        if dropped:
            summary += "\n\n_Changes to protected paths were discarded: " + ", ".join(f"`{p}`" for p in dropped) + "_"
        if passed is None:
            summary += "\n\n_No test command is configured for this project, so the change was not verified in the sandbox; rely on the MR pipeline._"
        self.result.update(title=described["title"], summary=summary, status="success" if passed is not False else "failed")
        return self.result

    def run(self) -> dict[str, Any]:
        self.setup()
        if self.agent["mode"] == "readonly":
            return self.run_readonly()
        return self.run_edit()
