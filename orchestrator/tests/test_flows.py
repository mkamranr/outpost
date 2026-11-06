"""Multi-agent flows: issue -> plan -> approval -> MR -> parallel review, plus stop/failure handling."""
import base64

import pytest
from fastapi.testclient import TestClient

from app.config import ProjectsConfig
from app.main import create_app
from app.registry import FlowDef, Registry
from app.tasks import STOPPED
from tests.conftest import ROOT
from tests.test_other_agents import NOTE_HEADERS, wire_common

ALL_AGENTS = ["pipeline-fixer", "planner", "code-writer", "code-reviewer", "mr-assist", "security-analyst", "test-writer"]


def issue_note(note, note_id):
    return {"object_kind": "note", "user": {"id": 7, "username": "alice"},
            "project": {"id": 42, "path_with_namespace": "group/app"},
            "object_attributes": {"id": note_id, "note": note, "noteable_type": "Issue", "discussion_id": None},
            "issue": {"iid": 11}}


@pytest.fixture
def flow_env(platform, fake):
    platform.projects = ProjectsConfig(
        defaults={"sandbox_image": "registry.internal/agent/sandbox-python:1", "test": ["pytest -q"]},
        allowed_image_prefixes=["registry.internal/"],
        projects={"group/app": {"agents": ALL_AGENTS}},
    )
    platform.settings.max_concurrent_sandboxes = 5
    wire_common(fake)
    fake.on("GET", "/projects/42/repository/branches/main", {"commit": {"id": "m1"}})
    fake.on("GET", "/projects/42/issues/11", {"iid": 11, "title": "Add multiply", "description": "We need mul()", "labels": []})
    notes: list = []
    fake.on("GET", "/projects/42/issues/11/notes", lambda req, m: notes)
    fake.on("POST", r"/projects/42/issues/11/notes", {"id": 900})
    fake.on("PUT", r"/projects/42/issues/11/notes/900", {"id": 900})
    fake.on("POST", r"/projects/42/issues/11/notes/\d+/award_emoji", {})
    # The MR the code-writer opens.
    fake.on("GET", "/projects/42/merge_requests/12", {
        "iid": 12, "title": "Add multiply", "source_branch": "agent/code-writer-issue-11-x", "target_branch": "main",
        "sha": "c2", "project_id": 42, "source_project_id": 42, "description": "Closes #11",
        "diff_refs": {"base_sha": "b0", "start_sha": "s0", "head_sha": "c2"}})
    fake.on("GET", "/projects/42/merge_requests/12/diffs", [{"old_path": "src/mul.py", "new_path": "src/mul.py", "diff": "+x"}])
    fake.on("POST", r"/projects/42/merge_requests/12/(notes|discussions)", {"id": 1})
    return TestClient(create_app(platform)), notes


def run_sandboxes(platform, fake, client, results):
    """Dispatch queued tasks, answer each sandbox with results[agent], then apply. Returns {agent: bundle}."""
    before = len(fake.posted(r"/sandbox/pipeline"))
    platform.run_once()
    bundles = {}
    for trigger in fake.posted(r"/sandbox/pipeline")[before:]:
        v = {x["key"]: x["value"] for x in trigger["variables"]}
        auth = {"Authorization": f"Bearer {v['AGENT_TASK_TICKET']}"}
        bundles[v["AGENT_NAME"]] = client.get(f"/api/tasks/{v['AGENT_TASK_ID']}/bundle", headers=auth).json()
        assert client.post(f"/api/tasks/{v['AGENT_TASK_ID']}/result", json=results[v["AGENT_NAME"]],
                           headers=auth).status_code == 200
    platform.run_once()
    return bundles


def status_note(fake):
    edits = [b["body"] for m, p, b in fake.calls if m == "PUT" and p.endswith("/issues/11/notes/900")]
    return edits[-1]


def test_issue_to_mr_flow(platform, fake, flow_env):
    client, notes = flow_env
    client.post("/webhook", json=issue_note("@agent build", 5001), headers=NOTE_HEADERS)
    first_note = fake.posted(r"/issues/11/notes$")[0]["body"]
    assert first_note.startswith("<!-- agent:flow -->") and "issue-to-mr" in first_note

    bundles = run_sandboxes(platform, fake, client, {"planner": {"status": "success", "body": "1. Add mul()"}})
    assert set(bundles) == {"planner"}
    plan = [b["body"] for b in fake.posted(r"/issues/11/notes$") if b["body"].startswith("<!-- agent:plan -->")][-1]
    notes.append({"author": {"username": "agent-bot"}, "body": plan, "system": False})
    assert "approve" in status_note(fake) and "⏸️" in status_note(fake)
    platform.run_once()
    assert len(fake.posted(r"/sandbox/pipeline")) == 1  # nothing runs while waiting for approval

    client.post("/webhook", json=issue_note("@agent approve but name it multiply", 5002), headers=NOTE_HEADERS)
    change = {"action": "create", "path": "src/mul.py", "content_b64": base64.b64encode(b"def multiply(): ...").decode()}
    bundles = run_sandboxes(platform, fake, client, {
        "code-writer": {"status": "success", "changes": [change], "tests_passed": True, "summary": "Added multiply()"}})
    prompt = bundles["code-writer"]["prompts"]["main"]
    assert "name it multiply" in prompt and "Add mul()" in prompt  # corrections + approved plan
    assert "Earlier steps of the `issue-to-mr` flow" in prompt

    bundles = run_sandboxes(platform, fake, client, {
        "code-reviewer": {"status": "success", "summary": "LGTM", "comments": []},
        "security-analyst": {"status": "success", "body": "No findings."},
    })
    assert set(bundles) == {"code-reviewer", "security-analyst"}  # parallel step
    assert all(b["repo"]["ref"] == "agent/code-writer-issue-11-x" for b in bundles.values())  # on the produced MR
    assert any("No findings." in b["body"] for b in fake.posted(r"/merge_requests/12/notes$"))

    run = platform.store.get_run(platform.store.recent()[0].flow_run_id)
    assert run.status == "done"
    final = status_note(fake)
    assert "finished" in final and final.count("✅") == 5 and "stop" not in final


def test_failed_step_stops_the_flow(platform, fake, flow_env):
    client, _ = flow_env
    client.post("/webhook", json=issue_note("@agent build", 5001), headers=NOTE_HEADERS)
    run_sandboxes(platform, fake, client, {"planner": {"status": "success", "body": "plan"}})
    client.post("/webhook", json=issue_note("@agent approve", 5002), headers=NOTE_HEADERS)
    run_sandboxes(platform, fake, client, {"code-writer": {"status": "no_change", "summary": "could not"}})
    run = platform.store.get_run(platform.store.recent()[0].flow_run_id)
    assert run.status == STOPPED and "code-writer" in run.outcome
    assert {t.agent for t in platform.store.recent()} == {"planner", "code-writer"}  # reviewers never started
    assert "stopped" in status_note(fake)


def test_stop_command_and_single_run_per_subject(platform, fake, flow_env):
    client, _ = flow_env
    client.post("/webhook", json=issue_note("@agent build", 5001), headers=NOTE_HEADERS)
    client.post("/webhook", json=issue_note("@agent build", 5003), headers=NOTE_HEADERS)
    assert "already running" in fake.posted(r"/issues/11/notes$")[-1]["body"]
    client.post("/webhook", json=issue_note("@agent stop", 5004), headers=NOTE_HEADERS)
    run_sandboxes(platform, fake, client, {"planner": {"status": "success", "body": "plan"}})
    runs = {t.flow_run_id for t in platform.store.recent()}
    assert len(runs) == 1 and platform.store.get_run(runs.pop()).status == STOPPED
    assert len(platform.store.recent()) == 1  # the planner finished but nothing came after it


def test_approve_without_flow_still_runs_code_writer(platform, fake, flow_env):
    client, _ = flow_env
    client.post("/webhook", json=issue_note("@agent approve", 5002), headers=NOTE_HEADERS)
    assert [t.agent for t in platform.store.recent()] == ["code-writer"]


def test_approval_needs_developer_access(platform, fake, flow_env):
    import httpx
    client, _ = flow_env
    client.post("/webhook", json=issue_note("@agent build", 5001), headers=NOTE_HEADERS)
    run_sandboxes(platform, fake, client, {"planner": {"status": "success", "body": "plan"}})
    fake.on("GET", "/projects/42/members/all/7", httpx.Response(404))
    client.post("/webhook", json=issue_note("@agent approve", 5002), headers=NOTE_HEADERS)
    assert platform.store.get_run(platform.store.recent()[0].flow_run_id).status == "waiting_approval"


def test_help_lists_flows(platform, fake, flow_env):
    client, _ = flow_env
    client.post("/webhook", json=issue_note("@agent help", 5005), headers=NOTE_HEADERS)
    reply = fake.posted(r"/issues/11/notes$")[-1]["body"]
    assert "**Flows**" in reply and "`@agent build` on issue" in reply


def test_registry_loads_flows_and_rejects_bad_ones(registry, tmp_path):
    assert {"issue-to-mr", "fix-and-review", "mr-deep-review"} <= set(registry.flows)
    with pytest.raises(ValueError, match="gate"):
        FlowDef(name="x", description="d", triggers=[{"event": "note", "commands": ["x"]}], steps=[{"gate": "approval"}])
    with pytest.raises(ValueError, match="commands or labels"):
        FlowDef(name="x", description="d", triggers=[{"event": "pipeline", "status": "failed"}], steps=[{"agent": "a"}])
    # produced_mr after an agent that never opens an MR
    agents_dir = tmp_path / "agents"
    (agents_dir / "flows").mkdir(parents=True)
    (agents_dir / "prompts").mkdir()
    for src in (ROOT / "agents").glob("*.yaml"):
        (agents_dir / src.name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    for src in (ROOT / "agents" / "prompts").iterdir():
        (agents_dir / "prompts" / src.name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    (agents_dir / "flows" / "bad.yaml").write_text(
        "name: bad\ndescription: d\ntriggers: [{event: note, commands: [bad]}]\n"
        "steps:\n  - agent: planner\n  - agent: code-reviewer\n    target: produced_mr\n", encoding="utf-8")
    with pytest.raises(ValueError, match="produced_mr"):
        Registry.load(agents_dir)
