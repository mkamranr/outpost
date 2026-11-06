"""The chat agent: runs inside the orchestrator with GitLab tools, replies in the thread, hands work to specialists."""
import json

import pytest
from fastapi.testclient import TestClient

from agent_runner.llm import LLM, LLMConfig
from app.config import ProjectsConfig
from app.main import create_app
from app.platform import Platform
from app.tasks import DISPATCHED, DONE, TaskStore
from tests.conftest import load_fixture
from tests.test_other_agents import NOTE_HEADERS, wire_common


class ScriptedLLM(LLM):
    def __init__(self, replies):
        super().__init__(LLMConfig(base_url="http://vllm", model="qwen"), transport=self._next)
        self.replies = list(replies)
        self.requests = []

    def _next(self, payload):
        self.requests.append(payload)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return {"choices": [{"message": reply}]}


def call(name, args, cid="c1"):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


@pytest.fixture
def chat_env(settings, registry, fake):
    def make(*replies):
        llm = ScriptedLLM(replies)
        projects = ProjectsConfig(
            defaults={"sandbox_image": "registry.internal/agent/sandbox-python:1"},
            allowed_image_prefixes=["registry.internal/"],
            projects={"group/app": {"agents": ["chat", "code-reviewer", "mr-assist", "security-analyst",
                                               "pipeline-fixer"]}},
        )
        client = fake.client()
        platform = Platform(settings, registry, projects, TaskStore(settings.database_url),
                            gitlab_factory=lambda ref: client, llm_factory=lambda: llm)
        wire_common(fake)
        fake.on("GET", "/projects/42/merge_requests/5/discussions/disc123", {"notes": [
            {"author": {"username": "alice"}, "body": "Why did the last pipeline fail?", "system": False},
            {"author": {"username": "agent-bot"}, "body": "Which pipeline do you mean?", "system": False},
            {"author": {"username": "alice"}, "body": "@agent the one on this MR", "system": False}]})
        fake.on("POST", r"/projects/42/merge_requests/5/notes/\d+/award_emoji", {})
        return platform, llm, TestClient(create_app(platform))
    return make


def ask(client, text="@agent why did the pipeline fail?", note_id=3010):
    payload = load_fixture("note_mr_fix.json")
    payload["object_attributes"].update(note=text, id=note_id, position=None)
    client.post("/webhook", json=payload, headers=NOTE_HEADERS)


def replies(fake):
    return [b["body"] for b in fake.posted(r"/merge_requests/5/discussions/disc123/notes$")]


def test_free_text_goes_to_chat_and_uses_tools(chat_env, fake):
    platform, llm, client = chat_env(
        call("get_job_log", {"job_id": 77}),
        call("submit", {"body": "Job 77 failed: `test_add` asserts 3 == 4."}, cid="c2"),
    )
    fake.on("GET", "/projects/42/jobs/77/trace", "FAILED tests/test_calc.py::test_add - assert 3 == 4\n")
    ask(client)
    platform.run_once()

    assert not fake.posted(r"/sandbox/pipeline")  # no sandbox for chat
    assert "Job 77 failed" in replies(fake)[-1]
    task = platform.store.recent()[0]
    assert task.agent == "chat" and task.status == DONE and task.instruction == "why did the pipeline fail?"
    prompt = llm.requests[0]["messages"][1]["content"]
    assert "Which pipeline do you mean?" in prompt and "@agent-bot is you" in prompt  # the thread is the session
    assert "| `code-reviewer` | agent (read-only)" in prompt and "| `mr-deep-review` | flow" not in prompt
    tool_reply = [m for m in llm.requests[1]["messages"] if m["role"] == "tool"][0]["content"]
    assert "assert 3 == 4" in tool_reply and tool_reply.startswith("<untrusted-data")


def test_readonly_handoff_starts_the_agent(chat_env, fake):
    platform, _, client = chat_env(call("submit", {
        "body": "Sure.", "handoff": {"target": "code-reviewer", "instruction": "focus on calc.py"}}))
    ask(client, "@agent can you review this MR?")
    platform.run_once()
    assert "I've asked the `code-reviewer` agent" in replies(fake)[-1]
    review = [t for t in platform.store.recent() if t.agent == "code-reviewer"]
    assert len(review) == 1 and review[0].instruction == "focus on calc.py"
    platform.run_once()
    assert fake.posted(r"/sandbox/pipeline")  # the reviewer runs in the sandbox as usual


def test_edit_handoff_only_suggests_the_command(chat_env, fake):
    platform, _, client = chat_env(call("submit", {
        "body": "That needs a code change.", "handoff": {"target": "mr-assist", "instruction": "use <= in add"}}))
    ask(client, "@agent please change add to use <=")
    platform.run_once()
    reply = replies(fake)[-1]
    assert "@agent fix use <= in add" in reply
    assert [t.agent for t in platform.store.recent()] == ["chat"]  # nothing that edits code was started


def test_unknown_or_disabled_handoff_is_ignored(chat_env, fake):
    platform, _, client = chat_env(call("submit", {"body": "Here you go.", "handoff": {"target": "planner"}}))
    ask(client, "@agent hello")
    platform.run_once()
    assert replies(fake)[-1].startswith("Here you go.") and "planner" not in replies(fake)[-1]


def test_llm_failure_is_reported(chat_env, fake):
    platform, _, client = chat_env(RuntimeError("LLM endpoint unavailable"))
    ask(client, "@agent hi")
    platform.run_once()
    assert "I could not answer that" in replies(fake)[-1]


def test_chat_is_not_queued_behind_sandbox_jobs(chat_env, fake):
    platform, _, client = chat_env(call("submit", {"body": "Answer."}))
    platform.settings.max_concurrent_sandboxes = 1
    busy = platform.store.create(agent="code-reviewer", project_id=42, project_path="group/app", dedupe_key="busy",
                                 subject_key="s", target={})
    platform.store.update(busy.id, status=DISPATCHED)
    ask(client, "@agent quick question")
    platform.run_once()
    assert replies(fake)[-1].startswith("Answer.")


def test_chat_disabled_falls_back_to_unknown_command(platform, fake):
    wire_common(fake)
    ask(TestClient(create_app(platform)), "@agent what is this?")
    assert "I don't know the command `what`" in fake.posted(r"/discussions/disc123/notes$")[-1]["body"]
