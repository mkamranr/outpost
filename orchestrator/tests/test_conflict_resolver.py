"""`@agent rebase`: GitLab rebase when there are no conflicts, a replacement Draft MR when there are."""
import base64

import pytest
from fastapi.testclient import TestClient

from app import conflicts
from app.config import ProjectsConfig
from app.main import create_app
from app.tasks import DONE
from tests.conftest import load_fixture
from tests.test_other_agents import HEAD, NOTE_HEADERS, run_to_result, wire_common

TARGET_SHA = "t" * 40


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch):
    monkeypatch.setattr(conflicts, "REBASE_POLL_SECONDS", 0)


def setup(platform, fake, source_branch="feature/x", **mr):
    platform.projects = ProjectsConfig(
        defaults={"sandbox_image": "registry.internal/agent/sandbox-python:1", "test": ["pytest -q"]},
        allowed_image_prefixes=["registry.internal/"],
        projects={"group/app": {"agents": ["conflict-resolver"]}},
    )
    wire_common(fake, source_branch=source_branch)
    state = {"iid": 5, "title": "Draft: Add calc", "source_branch": source_branch, "target_branch": "main",
             "sha": HEAD, "project_id": 42, "source_project_id": 42, "state": "opened", "description": "adds calc",
             "detailed_merge_status": "mergeable", "has_conflicts": False, "diverged_commits_count": 3,
             "diff_refs": {"base_sha": "b0", "start_sha": "s0", "head_sha": HEAD}, **mr}
    fake.on("GET", "/projects/42/merge_requests/5", lambda req, m: state)
    fake.on("GET", "/projects/42/repository/branches/main", {"commit": {"id": TARGET_SHA}})
    fake.on("POST", r"/projects/42/merge_requests/5/notes/\d+/award_emoji", {})
    return state


def rebase_comment():
    payload = load_fixture("note_mr_fix.json")
    payload["object_attributes"].update(note="@agent rebase", discussion_id=None, position=None, id=3010)
    return payload


def send(platform, client):
    client.post("/webhook", json=rebase_comment(), headers=NOTE_HEADERS)
    platform.run_once()


def last_note(fake):
    return fake.posted(r"/merge_requests/5/notes$")[-1]["body"]


def test_no_conflicts_on_an_agent_branch_uses_gitlab_rebase(platform, fake):
    state = setup(platform, fake, source_branch="agent/code-writer-issue-3-abcdef")

    def rebase(req, m):
        state["rebase_in_progress"] = False
        return {"rebase_in_progress": True}
    fake.on("PUT", "/projects/42/merge_requests/5/rebase", rebase)
    client = TestClient(create_app(platform))
    send(platform, client)
    assert fake.posted(r"/merge_requests/5/rebase$") == [{}]
    assert "Rebased `agent/code-writer-issue-3-abcdef` onto `main` (3 commit(s))" in last_note(fake)
    assert not fake.posted(r"/sandbox/pipeline")  # no sandbox, no model
    assert platform.store.recent()[0].status == DONE


def test_rebase_errors_are_reported(platform, fake):
    state = setup(platform, fake, source_branch="agent/x-1-abcdef")
    fake.on("PUT", "/projects/42/merge_requests/5/rebase",
            lambda req, m: state.update(rebase_in_progress=False, merge_error="Rebase failed: protected") or {})
    send(platform, TestClient(create_app(platform)))
    assert "GitLab could not rebase" in last_note(fake) and "protected" in last_note(fake)


def test_no_conflicts_on_a_human_branch_points_to_the_rebase_button(platform, fake):
    setup(platform, fake)
    send(platform, TestClient(create_app(platform)))
    assert "**Rebase** button" in last_note(fake)
    assert not fake.posted(r"/rebase$") and not fake.posted(r"/sandbox/pipeline")


@pytest.mark.parametrize("mr, expected", [
    ({"diverged_commits_count": 0}, "is up to date with `main`: nothing to do"),
    ({"source_project_id": 99}, "comes from a fork"),
    ({"detailed_merge_status": "checking"}, "still checking"),
    ({"state": "merged"}, "only open merge requests"),
])
def test_nothing_to_do_cases(platform, fake, mr, expected):
    setup(platform, fake, **mr)
    send(platform, TestClient(create_app(platform)))
    assert expected in last_note(fake)
    assert not fake.posted(r"/sandbox/pipeline")


def test_conflicts_are_resolved_in_a_replacement_mr(platform, fake):
    setup(platform, fake, has_conflicts=True, detailed_merge_status="conflict")
    client = TestClient(create_app(platform))
    change = {"action": "update", "path": "src/calc.py", "content_b64": base64.b64encode(b"resolved\n").decode()}
    task_id, bundle = run_to_result(platform, fake, client, rebase_comment(), NOTE_HEADERS, {
        "status": "success", "summary": "Merged `main` and resolved conflicts in 1 file(s).", "changes": [change],
        "tests_passed": True, "conflicts": ["src/calc.py"]})
    assert bundle["repo"]["merge_target"] == {"ref": "main", "sha": TARGET_SHA}
    assert bundle["repo"]["sha"] == HEAD and bundle["agent"]["output"] == "conflict_resolution"

    commit = fake.posted(r"/repository/commits$")[-1]
    assert commit["branch"].startswith("agent/conflict-resolver-mr-5-")
    assert commit["start_sha"] == TARGET_SHA  # cut from the target, not pushed onto the human's branch
    new_mr = fake.posted(r"/merge_requests$")[-1]
    assert new_mr["target_branch"] == "main" and new_mr["title"] == "Draft: Add calc (conflicts resolved)"
    assert "Replaces !5" in new_mr["description"] and "`src/calc.py`" in new_mr["description"]
    note = last_note(fake)
    assert "in !12, a replacement for this merge request" in note
    assert "Nothing on `feature/x` was changed" in note
    assert platform.store.get(task_id).status == DONE


def test_failed_resolution_explains_what_was_tried(platform, fake):
    setup(platform, fake, has_conflicts=True)
    client = TestClient(create_app(platform))
    run_to_result(platform, fake, client, rebase_comment(), NOTE_HEADERS, {
        "status": "failed", "summary": "Conflict markers are still left in: `src/calc.py`",
        "attempts": [{"n": 1, "tests_passed": None, "note": "conflict markers left in src/calc.py"}]})
    assert "I could not resolve the merge conflicts" in last_note(fake)
    assert "conflict markers left in src/calc.py" in last_note(fake)
    assert not fake.posted(r"/repository/commits$")
