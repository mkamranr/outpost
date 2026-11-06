import base64
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_runner import repo as git_repo
from agent_runner.engines import EngineRun
from agent_runner.explore import explore
from agent_runner.llm import LLM, LLMConfig, extract_json
from agent_runner.runner import Runner, candidate_files

PY = Path(sys.executable).as_posix()


def make_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b + 1\n")
    (repo / "test_calc.py").write_text("from src.calc import add\nassert add(1, 2) == 3\n")
    (repo / ".gitlab").mkdir()
    (repo / ".gitlab" / "agent.yml").write_text("agents: [pipeline-fixer]\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"], cwd=repo, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    return repo, sha


class ScriptedLLM(LLM):
    """Returns canned assistant messages in order."""

    def __init__(self, replies):
        super().__init__(LLMConfig(base_url="http://vllm", model="qwen"), transport=self._next)
        self.replies = list(replies)
        self.requests = []

    def _next(self, payload):
        self.requests.append(payload)
        message = self.replies.pop(0)
        if isinstance(message, str):
            message = {"role": "assistant", "content": message}
        return {"choices": [{"message": message}]}


def tool_call(name, args, cid="c1"):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


class FixingEngine:
    """Stands in for Aider: first attempt makes a wrong edit, second fixes it."""

    def __init__(self):
        self.instructions = []

    def run(self, repo, instruction, files, attempt, timeout):
        self.instructions.append((instruction, files))
        body = "    return a - b\n" if attempt == 1 else "    return a + b\n"
        (repo / "src" / "calc.py").write_text("def add(a, b):\n" + body)
        (repo / ".gitlab" / "agent.yml").write_text("agents: []\n")  # protected: must be dropped
        return EngineRun(ok=True)


def bundle(sha, **agent):
    return {
        "task_id": "t1",
        "agent": {"name": "pipeline-fixer", "mode": "edit", "engine": "aider", "output": "pipeline_fix",
                  "classify": True, "max_attempts": 3, **agent},
        "prompts": {"classify": "classify this", "main": "fix the failure"},
        "repo": {"path": "group/app", "http_url": "https://x/group/app.git", "ref": "main", "sha": sha},
        "project": {"setup": [], "test": [f"{PY} test_calc.py"], "protected_paths": [".gitlab/agent.yml"]},
        "context": {"failed_jobs": [{"trace": "File \"src/calc.py\", line 2\nAssertionError"}]},
    }


@pytest.fixture(autouse=True)
def need_bash():
    try:
        ok = subprocess.run(["bash", "-lc", "echo ok"], capture_output=True, text=True, timeout=30).stdout.strip() == "ok"
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    if not ok:
        pytest.skip("bash is required to run project commands")


def test_edit_loop_retries_until_tests_pass(tmp_path):
    repo, sha = make_repo(tmp_path)
    llm = ScriptedLLM([
        '{"classification": "code", "reason": "add() is off by one", "suspect_files": ["src/calc.py"]}',
        '```json\n{"title": "Fix add()", "summary": "Removed the stray +1."}\n```',
    ])
    engine = FixingEngine()
    work = tmp_path / "work"
    work.mkdir()
    result = Runner(bundle(sha), repo, work, llm.config, time.time() + 600, llm=llm, engine=engine).run()

    assert result["status"] == "success" and result["tests_passed"] is True
    assert [a["tests_passed"] for a in result["attempts"]] == [False, True]
    assert "AssertionError" in engine.instructions[1][0]  # test output fed back on retry
    assert engine.instructions[0][1][0] == "src/calc.py"  # suspect files passed to the engine
    paths = {c["path"]: c for c in result["changes"]}
    assert set(paths) == {"src/calc.py"}  # protected file dropped, test artifacts (__pycache__) ignored
    content = base64.b64decode(paths["src/calc.py"]["content_b64"]).decode()
    assert content.replace("\r\n", "\n").endswith("return a + b\n")  # write_text uses CRLF on Windows
    assert result["title"] == "Fix add()" and "protected paths were discarded" in result["summary"]


def test_flaky_classification_short_circuits(tmp_path):
    repo, sha = make_repo(tmp_path)
    llm = ScriptedLLM(['{"classification": "flaky", "reason": "runner lost"}'])
    result = Runner(bundle(sha), repo, tmp_path, llm.config, time.time() + 600, llm=llm, engine=FixingEngine()).run()
    assert result == {**result, "status": "success", "classification": "flaky", "changes": []}


def test_gives_up_after_max_attempts(tmp_path):
    repo, sha = make_repo(tmp_path)

    class BadEngine:
        def run(self, repo, instruction, files, attempt, timeout):
            (repo / "src" / "calc.py").write_text("def add(a, b):\n    return 0\n")
            return EngineRun(ok=True)

    llm = ScriptedLLM(['{"classification": "code"}', '{"title": "t", "summary": "s"}'])
    result = Runner(bundle(sha, max_attempts=2), repo, tmp_path, llm.config, time.time() + 600, llm=llm,
                    engine=BadEngine()).run()
    assert result["status"] == "failed" and len(result["attempts"]) == 2


def test_readonly_review_tool_loop(tmp_path):
    repo, sha = make_repo(tmp_path)
    llm = ScriptedLLM([
        tool_call("grep", {"pattern": "def add"}),
        tool_call("read_file", {"path": "../../etc/passwd"}, cid="c2"),
        tool_call("submit", {"summary": "Bug found", "comments": [{"path": "src/calc.py", "line": 2, "body": "+1"}]}, cid="c3"),
    ])
    b = bundle(sha, mode="readonly", engine="llm", output="review", classify=False)
    result = Runner(b, repo, tmp_path, llm.config, time.time() + 600, llm=llm).run()
    assert result["status"] == "success" and result["comments"][0]["line"] == 2
    tool_msgs = [m for m in llm.requests[-1]["messages"] if m["role"] == "tool"]
    assert "src/calc.py:1:def add" in tool_msgs[0]["content"]
    assert "outside repository" in tool_msgs[1]["content"]


def test_explore_accepts_plain_json_answer(tmp_path):
    repo, _ = make_repo(tmp_path)
    llm = ScriptedLLM(['<think>hmm</think>{"body": "## Plan\\n1. do it"}'])
    assert explore(llm, repo, "plan", "note", time.time() + 600) == {"body": "## Plan\n1. do it"}


def test_extract_json_variants():
    assert extract_json('Sure! {"a": 1} trailing') == {"a": 1}
    assert extract_json('```json\n{"a": {"b": "}"}}\n```') == {"a": {"b": "}"}}
    assert extract_json("no json here") is None


def test_candidate_files_from_trace(tmp_path):
    repo, sha = make_repo(tmp_path)
    assert candidate_files(bundle(sha), repo, ["missing.py"]) == ["src/calc.py"]


def test_collect_changes_handles_create_delete_move(tmp_path):
    repo, sha = make_repo(tmp_path)
    (repo / "new.txt").write_text("hi")
    (repo / "test_calc.py").unlink()
    subprocess.run(["git", "mv", "src/calc.py", "src/calculator.py"], cwd=repo, check=True)
    changes, dropped = git_repo.collect_changes(repo, sha, [])
    actions = {(c["action"], c["path"]) for c in changes}
    assert ("create", "new.txt") in actions and ("delete", "test_calc.py") in actions
    assert ("move", "src/calculator.py") in actions and dropped == []


def test_authenticated_url():
    assert git_repo.authenticated_url("https://gitlab.local:8443/g/p.git", "tok/en") == \
        "https://gitlab-ci-token:tok%2Fen@gitlab.local:8443/g/p.git"


JOB_LOG_TOOL = {"name": "get_job_log", "description": "job log",
                "parameters": {"type": "object", "properties": {"job_id": {"type": "integer"}}, "required": ["job_id"]}}


def test_explore_uses_remote_tools(tmp_path):
    from agent_runner.remote import RemoteTools
    repo, _ = make_repo(tmp_path)
    remote = RemoteTools.canned([JOB_LOG_TOOL], {"get_job_log": "AssertionError in test_add"})
    llm = ScriptedLLM([
        tool_call("get_job_log", {"job_id": 9}),
        tool_call("submit", {"body": "The job failed on test_add."}, cid="c2"),
    ])
    assert explore(llm, repo, "why did it fail?", "note", time.time() + 600, remote=remote)["body"].startswith("The job")
    offered = {t["function"]["name"] for t in llm.requests[0]["tools"]}
    assert offered == {"list_files", "read_file", "grep", "get_job_log", "submit"}
    assert "AssertionError" in [m for m in llm.requests[-1]["messages"] if m["role"] == "tool"][0]["content"]


def test_explore_without_checkout_offers_only_remote_tools():
    from agent_runner.remote import RemoteTools
    remote = RemoteTools.canned([JOB_LOG_TOOL], {})
    llm = ScriptedLLM([tool_call("read_file", {"path": "x"}),
                       tool_call("submit", {"body": "hi", "handoff": {"target": "code-reviewer"}}, cid="c2")])
    answer = explore(llm, None, "hello", "chat", time.time() + 600, remote=remote)
    assert answer["handoff"]["target"] == "code-reviewer"
    assert {t["function"]["name"] for t in llm.requests[0]["tools"]} == {"get_job_log", "submit"}
    assert "unknown tool" in [m for m in llm.requests[-1]["messages"] if m["role"] == "tool"][0]["content"]


def test_edit_agent_investigates_before_editing(tmp_path):
    from agent_runner.remote import RemoteTools
    repo, sha = make_repo(tmp_path)
    remote = RemoteTools.canned([JOB_LOG_TOOL], {"get_job_log": "assert add(1, 2) == 3"})
    llm = ScriptedLLM([
        '{"classification": "code", "reason": "off by one", "suspect_files": []}',
        tool_call("get_job_log", {"job_id": 1}),
        tool_call("submit", {"findings": "add() adds an extra 1", "suspect_files": ["src/calc.py"]}, cid="c2"),
        '{"title": "Fix add()", "summary": "s"}',
    ])
    engine = FixingEngine()
    b = bundle(sha, max_attempts=2)
    result = Runner(b, repo, tmp_path, llm.config, time.time() + 600, llm=llm, engine=engine, remote=remote).run()
    assert result["status"] == "success"
    first_instruction, first_files = engine.instructions[0]
    assert "add() adds an extra 1" in first_instruction and first_files[0] == "src/calc.py"
