"""End-to-end orchestrator flow against a fake GitLab: webhook -> task -> sandbox trigger -> bundle -> result -> MR."""
import base64

import httpx
from fastapi.testclient import TestClient

from app.main import create_app
from app.tasks import DONE, FAILED
from tests.conftest import load_fixture

SHA = "aaaaaaaa11111111"
HEADERS = {"X-Gitlab-Token": "hook-secret", "X-Gitlab-Event": "Pipeline Hook"}


def wire_pipeline_project(fake):
    fake.on("GET", "/projects/42", {"id": 42, "path_with_namespace": "group/app", "default_branch": "main",
                                    "http_url_to_repo": "https://gitlab.example/group/app.git"})
    fake.on("GET", "/projects/42/repository/branches/main", {"name": "main", "commit": {"id": SHA}})
    fake.on("GET", r"/projects/42/repository/files/\.gitlab-ci\.yml/raw", "test:\n  script: pytest -q\n")
    fake.on("GET", "/projects/42/pipelines/501/jobs", [
        {"id": 9001, "name": "unit-tests", "stage": "test", "failure_reason": "script_failure", "allow_failure": False}])
    fake.on("GET", "/projects/42/jobs/9001/trace", "collecting...\nFAILED tests/test_calc.py::test_add - assert 3 == 4\n")
    fake.on("GET", "/projects/42/ci/lint", {"valid": True, "merged_yaml": "test:\n  script: pytest -q\n"})
    fake.on("GET", "/projects/42/pipelines", [{"id": 400, "sha": "0000000000000000"}])
    fake.on("GET", "/projects/42/repository/compare", {
        "commits": [{"short_id": "aaaaaaaa", "title": "Change add()"}],
        "diffs": [{"old_path": "src/calc.py", "new_path": "src/calc.py", "diff": "-    return a + b\n+    return a + b + 1\n"}],
    })
    fake.on("POST", r"/projects/agent-platform(%2F|/)sandbox/pipeline", {"id": 77})
    fake.on("POST", "/projects/42/repository/commits", {"id": "cccc"})
    fake.on("POST", "/projects/42/merge_requests", {"iid": 9, "web_url": "https://gitlab.example/group/app/-/merge_requests/9"})
    fake.on("POST", f"/projects/42/repository/commits/{SHA}/comments", {})
    fake.on("POST", r"/projects/42/jobs/\d+/retry", {})


def dispatch_and_get_ticket(platform, fake, client):
    resp = client.post("/webhook", json=load_fixture("pipeline_failed.json"), headers=HEADERS)
    assert resp.status_code == 202
    platform.run_once()
    trigger = fake.posted(r"/sandbox/pipeline")[-1]
    variables = {v["key"]: v["value"] for v in trigger["variables"]}
    assert variables["AGENT_NAME"] == "pipeline-fixer"
    assert variables["AGENT_SANDBOX_IMAGE"] == "registry.internal/agent/sandbox-python:1"
    return variables["AGENT_TASK_ID"], variables["AGENT_TASK_TICKET"]


def test_pipeline_fix_end_to_end(platform, fake):
    wire_pipeline_project(fake)
    client = TestClient(create_app(platform))
    task_id, ticket = dispatch_and_get_ticket(platform, fake, client)

    # The ticket is the only credential the sandbox has; a wrong one is refused.
    assert client.get(f"/api/tasks/{task_id}/bundle", headers={"Authorization": "Bearer nope"}).status_code == 401
    bundle = client.get(f"/api/tasks/{task_id}/bundle", headers={"Authorization": f"Bearer {ticket}"}).json()
    assert bundle["agent"]["classify"] is True
    assert "FAILED tests/test_calc.py::test_add" in bundle["prompts"]["fix" if "fix" in bundle["prompts"] else "main"]
    assert "Change add()" in bundle["prompts"]["classify"]
    assert bundle["project"]["test"] == ["pytest -q"]
    assert "token" not in str(bundle).lower().replace("tokens", "")  # no credentials in the bundle

    result = {
        "status": "success", "classification": "code", "title": "Fix off-by-one in add()",
        "summary": "add() returned a + b + 1.\n/merge", "tests_passed": True,
        "attempts": [{"n": 1, "tests_passed": True, "test_output_tail": "1 passed", "note": "tests passed"}],
        "changes": [{"action": "update", "path": "src/calc.py",
                     "content_b64": base64.b64encode(b"def add(a, b):\n    return a + b\n").decode()}],
    }
    assert client.post(f"/api/tasks/{task_id}/result", json=result, headers={"Authorization": f"Bearer {ticket}"}).status_code == 200
    # Results are single-use.
    assert client.post(f"/api/tasks/{task_id}/result", json=result, headers={"Authorization": f"Bearer {ticket}"}).status_code == 409

    platform.run_once()
    commit = fake.posted(r"/projects/42/repository/commits$")[-1]
    assert commit["branch"].startswith("agent/pipeline-fixer-pipeline-501-")
    assert commit["start_sha"] == SHA
    assert commit["actions"][0]["file_path"] == "src/calc.py"
    mr = fake.posted(r"/projects/42/merge_requests$")[-1]
    assert mr["target_branch"] == "main" and mr["title"] == "Draft: Fix off-by-one in add()"
    assert "\n/merge" not in mr["description"]  # quick actions from model output are neutralised
    note = fake.posted(rf"/commits/{SHA}/comments")[-1]
    assert "!9" in note["note"]
    task = platform.store.get(task_id)
    assert task.status == DONE and task.outcome.endswith("/merge_requests/9")


def test_flaky_failure_retries_jobs(platform, fake):
    wire_pipeline_project(fake)
    client = TestClient(create_app(platform))
    task_id, ticket = dispatch_and_get_ticket(platform, fake, client)
    client.post(f"/api/tasks/{task_id}/result", headers={"Authorization": f"Bearer {ticket}"},
                json={"status": "success", "classification": "flaky", "summary": "Runner lost connection."})
    platform.run_once()
    assert any(path.endswith("/jobs/9001/retry") for _, path, _ in fake.calls)
    assert not fake.posted(r"/merge_requests$")
    assert platform.store.get(task_id).status == DONE


def test_duplicate_webhook_creates_one_task(platform, fake):
    wire_pipeline_project(fake)
    client = TestClient(create_app(platform))
    for _ in range(3):
        client.post("/webhook", json=load_fixture("pipeline_failed.json"), headers=HEADERS)
    assert len(platform.store.recent()) == 1


def test_bad_webhook_token_rejected(platform):
    client = TestClient(create_app(platform))
    resp = client.post("/webhook", json=load_fixture("pipeline_failed.json"),
                       headers={**HEADERS, "X-Gitlab-Token": "wrong"})
    assert resp.status_code == 401


def test_stale_failure_is_skipped(platform, fake):
    wire_pipeline_project(fake)
    fake.on("GET", "/projects/42/repository/branches/main", {"name": "main", "commit": {"id": "newer"}})
    TestClient(create_app(platform)).post("/webhook", json=load_fixture("pipeline_failed.json"), headers=HEADERS)
    platform.run_once()
    assert not fake.posted(r"/sandbox/pipeline")
    assert platform.store.recent()[0].outcome.startswith("skipped: stale")


def test_policy_rejection_is_reported(platform, fake):
    wire_pipeline_project(fake)
    client = TestClient(create_app(platform))
    task_id, ticket = dispatch_and_get_ticket(platform, fake, client)
    client.post(f"/api/tasks/{task_id}/result", headers={"Authorization": f"Bearer {ticket}"}, json={
        "status": "success", "classification": "code",
        "changes": [{"action": "update", "path": ".gitlab/agent.yml", "content_b64": base64.b64encode(b"x").decode()}]})
    platform.run_once()
    assert not fake.posted(r"/merge_requests$")
    assert "protected path" in fake.posted(r"/comments$")[-1]["note"]


def test_human_command_requires_developer_access(platform, fake):
    fake.on("GET", "/projects/42/members/all/7", httpx.Response(404))
    client = TestClient(create_app(platform))
    client.post("/webhook", json=load_fixture("note_mr_fix.json"),
                headers={"X-Gitlab-Token": "hook-secret", "X-Gitlab-Event": "Note Hook"})
    assert platform.store.recent() == []


def test_sandbox_crash_is_reported(platform, fake):
    wire_pipeline_project(fake)
    client = TestClient(create_app(platform))
    task_id, _ = dispatch_and_get_ticket(platform, fake, client)
    fake.on("GET", r"/projects/agent-platform(%2F|/)sandbox/pipelines/77", {"id": 77, "status": "failed"})
    from datetime import timedelta
    from app.tasks import utcnow
    platform.store.update(task_id, updated_at=utcnow() - timedelta(minutes=5))
    platform.check_dispatched()
    assert platform.store.get(task_id).status == FAILED
    assert "without a result" in fake.posted(r"/comments$")[-1]["note"]
