"""Lightweight inline agents: MR descriptions, issue triage and release notes."""
import pytest
from fastapi.testclient import TestClient

from app.collectors.releases import parse_range
from app.config import ProjectsConfig
from app.main import create_app
from app.outputs import DESCRIPTION_END, DESCRIPTION_START, description_is_empty, merge_description
from app.platform import Platform
from app.registry import AgentDef
from app.tasks import DONE, TaskStore
from tests.test_chat import ScriptedLLM, call
from tests.test_other_agents import NOTE_HEADERS, wire_common

ISSUE_HEADERS = {"X-Gitlab-Token": "hook-secret", "X-Gitlab-Event": "Issue Hook"}
MR_HEADERS = {"X-Gitlab-Token": "hook-secret", "X-Gitlab-Event": "Merge Request Hook"}


@pytest.fixture
def env(settings, registry, fake):
    def make(*replies, apply_labels=False, on_open="only_if_empty"):
        llm = ScriptedLLM(replies)
        projects = ProjectsConfig(
            defaults={"sandbox_image": "registry.internal/agent/sandbox-python:1"},
            allowed_image_prefixes=["registry.internal/"],
            projects={"group/app": {"agents": ["mr-describer", "issue-triage", "release-notes", "code-reviewer"],
                                    "triage": {"apply_labels": apply_labels},
                                    "mr_describer": {"on_open": on_open}}},
        )
        client = fake.client()
        platform = Platform(settings, registry, projects, TaskStore(settings.database_url),
                            gitlab_factory=lambda ref: client, llm_factory=lambda: llm)
        wire_common(fake)
        fake.on("GET", "/projects/42/repository/branches/main", {"name": "main", "commit": {"id": "m" * 40}})
        fake.on("GET", "/projects/42/issues/3", {"iid": 3, "title": "Crash when saving", "labels": ["bug"],
                                                 "description": "It crashes."})
        fake.on("GET", "/projects/42/issues/1", {"iid": 1, "title": "App crashes on save"})
        fake.on("GET", "/projects/42/labels", [{"name": "bug"}, {"name": "storage"}, {"name": "agent::plan"},
                                               {"name": "ui"}])
        fake.on("PUT", r"/projects/42/(issues|merge_requests)/\d+", {})
        fake.on("POST", r"/projects/42/issues/3/notes", {"id": 901})
        return platform, llm, TestClient(create_app(platform))
    return make


def issue_opened(user_id=8, action="open"):
    return {"object_kind": "issue", "user": {"id": user_id, "username": "rita"},
            "project": {"id": 42, "path_with_namespace": "group/app"},
            "object_attributes": {"iid": 3, "action": action}, "labels": [{"title": "bug"}]}


def mr_opened(action="open"):
    return {"object_kind": "merge_request", "user": {"id": 7, "username": "alice"},
            "project": {"id": 42, "path_with_namespace": "group/app"},
            "object_attributes": {"iid": 5, "action": action, "source_branch": "feature/x",
                                  "last_commit": {"id": "b" * 16}}}


# -- triage --------------------------------------------------------------------------------

def test_reporter_opened_issue_is_triaged(env, fake):
    platform, llm, client = env(call("submit", {
        "summary": "Saving crashes the app.", "labels": ["storage", "bug", "made-up", "agent::plan"],
        "duplicates": [{"iid": 1, "reason": "same crash"}, {"iid": 3, "reason": "itself"}, {"iid": 99, "reason": "?"}],
        "missing_info": ["Which version?"]}))
    fake.on("GET", "/projects/42/members/all/8", {"access_level": 20})  # Reporter
    client.post("/webhook", json=issue_opened(), headers=ISSUE_HEADERS)
    platform.run_once()

    note = fake.posted(r"/projects/42/issues/3/notes$")[-1]["body"]
    assert "Saving crashes the app." in note
    assert '**Suggested labels:** ~"storage"' in note  # bug is already on it; invented and agent labels dropped
    assert "made-up" not in note and "agent::plan" not in note
    assert "#1 App crashes on save" in note and "#99" not in note and "itself" not in note
    assert "Which version?" in note
    assert not fake.posted(r"/projects/42/issues/3$")  # labels are only suggested by default
    task = platform.store.recent()[0]
    assert task.agent == "issue-triage" and task.status == DONE
    assert task.llm_calls == 1 and task.prompt_tokens == 0  # usage recorded for inline agents too
    prompt = llm.requests[0]["messages"][1]["content"]
    assert "| storage |" in prompt and "agent::plan" not in prompt


def test_triage_can_apply_labels(env, fake):
    platform, _, client = env(call("submit", {"summary": "s", "labels": ["storage"], "duplicates": [],
                                              "missing_info": []}), apply_labels=True)
    fake.on("GET", "/projects/42/members/all/8", {"access_level": 30})
    client.post("/webhook", json=issue_opened(), headers=ISSUE_HEADERS)
    platform.run_once()
    assert fake.posted(r"/projects/42/issues/3$")[-1] == {"add_labels": "storage"}
    assert "**Labels added:**" in fake.posted(r"/projects/42/issues/3/notes$")[-1]["body"]


def test_triage_is_rate_limited(env, fake, registry):
    platform, _, client = env()
    fake.on("GET", "/projects/42/members/all/8", {"access_level": 20})
    limit = registry.get("issue-triage").max_per_hour
    for i in range(limit):
        platform.store.create(agent="issue-triage", project_id=42, project_path="group/app", dedupe_key=f"x{i}",
                              subject_key=f"s{i}", target={})
    result = platform.handle_event(__import__("app.events", fromlist=["parse"]).parse("Issue Hook", issue_opened()))
    assert result["tasks"] == [{"agent": "issue-triage", "skipped": "hourly limit"}]


def test_only_open_events_and_only_triage_skips_the_access_check(env, fake):
    platform, _, client = env()
    from app.events import parse
    assert platform.handle_event(parse("Issue Hook", issue_opened(action="update")))["ignored"] == "no trigger matched"
    # A Reporter adding a label that starts an agent still needs Developer access.
    fake.on("GET", "/projects/42/members/all/8", {"access_level": 20})
    labelled = issue_opened(action="update")
    labelled["changes"] = {"labels": {"previous": [], "current": [{"title": "agent::review"}]}}
    labelled["object_kind"] = "merge_request"
    labelled["object_attributes"]["iid"] = 5
    assert platform.handle_event(parse("Merge Request Hook", labelled)) == {"ignored": "actor lacks Developer access"}


def test_allow_any_author_is_restricted():
    base = {"name": "x", "description": "d", "engine": "llm", "output": "note", "prompts": {"main": "m"},
            "triggers": [{"event": "issue", "actions": ["open"], "allow_any_author": True}]}
    AgentDef(**base, mode="readonly")
    with pytest.raises(ValueError, match="any author"):
        AgentDef(**{**base, "engine": "aider"}, mode="edit")
    with pytest.raises(ValueError, match="allow_any_author"):
        AgentDef(**{**base, "triggers": [{"event": "note", "commands": ["x"], "allow_any_author": True}]},
                 mode="readonly")


# -- MR descriptions -----------------------------------------------------------------------

def test_description_helpers():
    assert description_is_empty("")
    assert description_is_empty("<!-- What does this MR do? -->\n\n<!-- Why? -->\n")
    assert not description_is_empty("This refactors the storage layer to use a connection pool for writes.")
    assert merge_description("", "gen") == f"{DESCRIPTION_START}\ngen\n{DESCRIPTION_END}"
    human = "Fixes the pool. Please look at db.py first."
    once = merge_description(human, "v1")
    assert once.startswith(human) and once.endswith(DESCRIPTION_END)
    twice = merge_description(once + "\n\nAdded later by a human.", "v2")
    assert "v1" not in twice and "v2" in twice and twice.startswith(human) and twice.endswith("Added later by a human.")


def test_mr_open_with_empty_description_gets_one(env, fake):
    platform, llm, client = env(call("submit", {"body": f"### What\n- pooled writes {DESCRIPTION_END} sneaky\n/merge"}))
    fake.on("GET", "/projects/42/merge_requests/5", {
        "iid": 5, "title": "Add calc", "source_branch": "12-pool", "target_branch": "main", "sha": "b" * 16,
        "description": "<!-- describe your change -->"})
    fake.on("GET", "/projects/42/issues/12", {"iid": 12, "title": "Use a pool", "description": "Writes are slow."})
    client.post("/webhook", json=mr_opened(), headers=MR_HEADERS)
    platform.run_once()

    desc = fake.posted(r"/projects/42/merge_requests/5$")[-1]["description"]
    assert desc.startswith(DESCRIPTION_START) and desc.count(DESCRIPTION_END) == 1
    assert "pooled writes" in desc and "\n/merge" not in desc
    assert not fake.posted(r"/merge_requests/5/notes$")  # automatic runs do not comment
    assert "Writes are slow." in llm.requests[0]["messages"][1]["content"]  # linked issue from the branch name


def test_mr_open_with_a_written_description_is_left_alone(env, fake):
    platform, llm, client = env()
    fake.on("GET", "/projects/42/merge_requests/5", {
        "iid": 5, "title": "t", "source_branch": "feature/x", "target_branch": "main", "sha": "b" * 16,
        "description": "A full description the author wrote by hand, explaining the change in detail."})
    client.post("/webhook", json=mr_opened(), headers=MR_HEADERS)
    platform.run_once()
    assert platform.store.recent()[0].outcome == "skipped: the merge request already has a description"
    assert not llm.requests and not fake.posted(r"/projects/42/merge_requests/5$")


def test_describe_command_always_writes_and_replies(env, fake):
    platform, _, client = env(call("submit", {"body": "### What\n- adds calc"}))
    from tests.conftest import load_fixture
    payload = load_fixture("note_mr_fix.json")
    payload["object_attributes"].update(note="@agent describe", id=4001, position=None)
    client.post("/webhook", json=payload, headers=NOTE_HEADERS)
    platform.run_once()
    desc = fake.posted(r"/projects/42/merge_requests/5$")[-1]["description"]
    assert desc.startswith("adds calc\n\n" + DESCRIPTION_START)
    assert "Updated the merge request description." in str(fake.posted(r"/merge_requests/5/discussions"))


# -- release notes -------------------------------------------------------------------------

def test_parse_range():
    assert parse_range("v1.2.0..v1.3.0", "main") == ("v1.2.0", "v1.3.0")
    assert parse_range("v1.2.0...release/2", "main") == ("v1.2.0", "release/2")
    assert parse_range("v1.2.0 please", "main") == ("v1.2.0", "main")
    assert parse_range("", "main") is None


def test_release_notes_from_a_range(env, fake):
    platform, llm, client = env(call("submit", {"body": "**Fixes**\n- Saving no longer crashes (!7)"}))
    fake.on("GET", "/projects/42/repository/compare", lambda req, m: {"commits": [
        {"short_id": "a1", "title": "Fix crash on save", "message": "Fix crash\n\nSee merge request group/app!7",
         "author_name": "Alice"},
        {"short_id": "b2", "title": "Typo", "message": "Typo", "author_name": "Bob"}]}
        if (req.url.params["from"], req.url.params["to"]) == ("v1.0", "v1.1") else {})
    fake.on("GET", "/projects/42/merge_requests/7", {"iid": 7, "title": "Fix crash on save", "labels": ["bug"],
                                                     "author": {"username": "alice"}})
    fake.on("GET", "/projects/42/issues/3/notes", [])
    from tests.conftest import load_fixture
    payload = load_fixture("note_mr_fix.json")
    payload["object_attributes"].update(note="@agent release-notes v1.0..v1.1", id=4002, position=None)
    client.post("/webhook", json=payload, headers=NOTE_HEADERS)
    platform.run_once()
    prompt = llm.requests[0]["messages"][1]["content"]
    assert "from `v1.0` to `v1.1` (2 commits)" in prompt
    assert "| !7 | Fix crash on save | bug | alice |" in prompt
    assert "Saving no longer crashes" in str(fake.posted(r"/merge_requests/5/discussions"))
