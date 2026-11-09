"""Security remediator: reading scanner findings, routing, gates and the fix MR."""
import base64

import httpx
import pytest
from fastapi.testclient import TestClient

from app.collectors import CollectContext
from app.collectors.security import ignored_ids, vulnerabilities
from app.config import ProjectConfig, ProjectsConfig
from app.events import parse
from app.main import create_app
from app.platform import Platform
from app.router import route
from app.tasks import DONE, TaskStore
from tests.conftest import FIXTURES
from tests.test_platform_flow import SHA, wire_pipeline_project

PIPE_HEADERS = {"X-Gitlab-Token": "hook-secret", "X-Gitlab-Event": "Pipeline Hook"}
DB_PY = "import sqlite3\n\ndef find(cur, name):\n    cur.execute(f\"SELECT * FROM users WHERE name = '{name}'\")\n"


def wire_reports(fake, graphql=None):
    fake.on("GET", "/projects/42/pipelines/601", {"id": 601, "iid": 33, "status": "success"})
    fake.on("POST", "/graphql", graphql or {"errors": [{"message": "Field 'securityReportFindings' doesn't exist"}]},
            prefix="/api")
    fake.on("GET", "/projects/42/pipelines/601/jobs", [
        {"id": 5001, "name": "semgrep-sast", "artifacts": [{"file_type": "sast"}, {"file_type": "trace"}]},
        {"id": 5002, "name": "unit", "artifacts": [{"file_type": "trace"}]}])
    fake.on("GET", "/projects/42/jobs/5001/artifacts/gl-sast-report.json",
            lambda req, m: httpx.Response(200, content=(FIXTURES / "gl-sast-report.json").read_bytes()))
    fake.on("GET", r"/projects/42/repository/files/app(%2F|/)db\.py/raw", DB_PY)


def ctx_for(fake, target, project=None, budget=8000):
    return CollectContext(gl=fake.client(), target=target, project=project or ProjectConfig(), sha=SHA, ref="main",
                          budget_tokens=budget)


@pytest.fixture
def sec_env(settings, registry, fake):
    def make(auto=True, **policy):
        projects = ProjectsConfig(
            defaults={"sandbox_image": "registry.internal/agent/sandbox-python:1", "test": ["pytest -q"]},
            allowed_image_prefixes=["registry.internal/"],
            projects={"group/app": {"agents": ["pipeline-fixer", "security-remediator", "security-analyst"],
                                    "security_remediator": {"auto": auto, **policy}}},
        )
        client = fake.client()
        platform = Platform(settings, registry, projects, TaskStore(settings.database_url),
                            gitlab_factory=lambda ref: client)
        wire_pipeline_project(fake)
        wire_reports(fake)
        fake.on("GET", "/projects/42/merge_requests", [])
        return platform, TestClient(create_app(platform))
    return make


def pipeline_succeeded(ref="main"):
    return {"object_kind": "pipeline", "user": {"id": 7, "username": "alice"},
            "project": {"id": 42, "path_with_namespace": "group/app"},
            "object_attributes": {"id": 601, "status": "success", "ref": ref, "sha": SHA, "source": "push"},
            "builds": []}


# -- findings --------------------------------------------------------------------------------

def test_findings_from_report_files_are_filtered_and_annotated(fake):
    wire_reports(fake)
    fake.on("GET", "/projects/42", {"id": 42, "path_with_namespace": "group/app", "default_branch": "main"})
    fake.on("GET", r"/projects/42/repository/files/\.gitlab(%2F|/)agent-security-ignore\.yml/raw",
            "ignore:\n  - id: CWE-798\n    reason: test fixture credentials\n")
    out = vulnerabilities(ctx_for(fake, {"kind": "pipeline", "project_id": 42, "pipeline_id": 601}))
    assert out["total_findings"] == 4 and out["matching"] == 2  # medium dropped, critical ignored by CWE
    assert [f["id"] for f in out["findings"]] == ["f1", "f4"]
    first = out["findings"][0]
    assert first["severity"] == "high" and first["scanner"] == "sast" and "CWE-89" in first["identifiers"]
    assert ">" in first["code"] and "cur.execute" in first["code"]  # the flagged line is marked
    assert out["scope"] == "whole repository"


def test_findings_on_an_mr_are_limited_to_its_files(fake):
    wire_reports(fake)
    fake.on("GET", "/projects/42", {"id": 42, "path_with_namespace": "group/app", "default_branch": "main"})
    fake.on("GET", "/projects/42/merge_requests/5", {"iid": 5, "head_pipeline": {"id": 601}})
    fake.on("GET", "/projects/42/merge_requests/5/diffs", [{"new_path": "scripts/run.py"}])
    out = vulnerabilities(ctx_for(fake, {"kind": "note", "project_id": 42, "noteable_type": "merge_request", "iid": 5}))
    assert [f["id"] for f in out["findings"]] == ["f4"]


def test_graphql_findings_are_normalised(fake):
    nodes = [{"uuid": "u1", "title": "Vulnerable lodash", "severity": "CRITICAL", "reportType": "DEPENDENCY_SCANNING",
              "identifiers": [{"name": "CVE-2021-23337"}],
              "location": {"file": "package-lock.json", "dependency": {"package": {"name": "lodash"},
                                                                       "version": "4.17.20"}}}]
    fake.on("GET", "/projects/42", {"id": 42, "path_with_namespace": "group/app"})
    fake.on("GET", "/projects/42/pipelines/601", {"id": 601, "iid": 33})
    seen = []

    def graphql(req, m):
        import json
        seen.append(json.loads(req.content))
        return {"data": {"project": {"pipeline": {"securityReportFindings": {"nodes": nodes}}}}}
    fake.on("POST", "/graphql", graphql, prefix="/api")
    findings = fake.client().security_findings(42, 601)
    assert seen[0]["variables"] == {"path": "group/app", "iid": "33"}
    assert findings == [{"id": "u1", "scanner": "dependency_scanning", "severity": "critical",
                         "name": "Vulnerable lodash", "file": "package-lock.json", "package": "lodash",
                         "version": "4.17.20", "identifiers": ["CVE-2021-23337"]}]


def test_graphql_is_read_only(fake):
    with pytest.raises(ValueError, match="mutations"):
        fake.client().graphql("mutation { vulnerabilityDismiss(input: {}) { errors } }", {})
    assert not fake.calls


def test_ignore_file_formats():
    assert ignored_ids("- f1\n- id: CVE-1\n") == {"f1", "CVE-1"}
    assert ignored_ids("ignore: [f2]") == {"f2"}
    assert ignored_ids(": not yaml [") == set()


# -- routing and gates -----------------------------------------------------------------------

def test_auto_trigger_is_opt_in_and_branch_limited(registry):
    def matches(ref="main", **policy):
        project = ProjectConfig(agents=["pipeline-fixer", "security-remediator"], security_remediator=policy)
        event = parse("Pipeline Hook", pipeline_succeeded(ref))
        return [m.agent.name for m in route(event, registry, project, "agent-bot", "@agent").matches]

    assert matches() == []  # auto is off by default
    assert matches(auto=True) == ["security-remediator"]  # and the pipeline fixer ignores successes
    assert matches(ref="feature/x", auto=True) == []
    assert matches(ref="release/1", auto=True, branches=["release/*"]) == ["security-remediator"]


def test_open_mr_cap_pauses_automatic_runs(sec_env, fake):
    platform, client = sec_env(max_open_mrs=1)
    fake.on("GET", "/projects/42/merge_requests", lambda req, m: [{"iid": 3}]
            if req.url.params.get("labels") == "agent::security-fix" else [])
    client.post("/webhook", json=pipeline_succeeded(), headers=PIPE_HEADERS)
    platform.run_once()
    task = platform.store.recent()[0]
    assert task.outcome == "skipped: 1 security fix MRs are already open"
    assert not fake.posted(r"/sandbox/pipeline")


def test_pipeline_findings_become_a_draft_mr(sec_env, fake):
    platform, client = sec_env()
    client.post("/webhook", json=pipeline_succeeded(), headers=PIPE_HEADERS)
    platform.run_once()
    trigger = fake.posted(r"/sandbox/pipeline")[-1]
    v = {x["key"]: x["value"] for x in trigger["variables"]}
    assert v["AGENT_NAME"] == "security-remediator"
    auth = {"Authorization": f"Bearer {v['AGENT_TASK_TICKET']}"}
    bundle = client.get(f"/api/tasks/{v['AGENT_TASK_ID']}/bundle", headers=auth).json()
    assert "SQL injection via string formatting" in bundle["prompts"]["main"]
    assert ".gitlab/agent-security-ignore.yml" in bundle["project"]["protected_paths"]

    fixed = DB_PY.replace("f\"SELECT * FROM users WHERE name = '{name}'\"", "\"SELECT * FROM users WHERE name = ?\", (name,)")
    result = {"status": "success", "title": "Parameterise the user lookup", "summary": "f1: fixed (parameterised)",
              "tests_passed": True, "changes": [{"action": "update", "path": "app/db.py",
                                                 "content_b64": base64.b64encode(fixed.encode()).decode()}]}
    assert client.post(f"/api/tasks/{v['AGENT_TASK_ID']}/result", json=result, headers=auth).status_code == 200
    platform.run_once()

    commit = fake.posted(r"/projects/42/repository/commits$")[-1]
    assert commit["branch"].startswith("agent/security-remediator-vuln-601-") and commit["start_sha"] == SHA
    mr = fake.posted(r"/projects/42/merge_requests$")[-1]
    assert mr["labels"] == "agent::generated,agent::security-fix" and mr["target_branch"] == "main"
    assert "| high | SQL injection via string formatting | `app/db.py:4` | CWE-89 |" in mr["description"]
    assert platform.store.get(v["AGENT_TASK_ID"]).status == DONE


def test_policy_rejects_editing_the_ignore_file(sec_env, fake):
    platform, client = sec_env()
    client.post("/webhook", json=pipeline_succeeded(), headers=PIPE_HEADERS)
    platform.run_once()
    v = {x["key"]: x["value"] for x in fake.posted(r"/sandbox/pipeline")[-1]["variables"]}
    auth = {"Authorization": f"Bearer {v['AGENT_TASK_TICKET']}"}
    result = {"status": "success", "summary": "silenced", "changes": [
        {"action": "create", "path": ".gitlab/agent-security-ignore.yml",
         "content_b64": base64.b64encode(b"- f1\n").decode()}]}
    client.post(f"/api/tasks/{v['AGENT_TASK_ID']}/result", json=result, headers=auth)
    platform.run_once()
    assert not fake.posted(r"/projects/42/merge_requests$")
    assert platform.store.get(v["AGENT_TASK_ID"]).outcome.startswith("rejected:")


def test_remediate_without_findings_says_so(sec_env, fake):
    from tests.conftest import load_fixture
    from tests.test_other_agents import NOTE_HEADERS, wire_common

    platform, client = sec_env(auto=False)
    wire_common(fake)
    fake.on("GET", "/projects/42/merge_requests/5", {
        "iid": 5, "title": "Add calc", "source_branch": "feature/x", "target_branch": "main", "sha": SHA,
        "project_id": 42, "source_project_id": 42, "head_pipeline": {"id": 601}})
    fake.on("GET", "/projects/42/merge_requests/5/diffs", [{"new_path": "README.md"}])  # touches no flagged file
    payload = load_fixture("note_mr_fix.json")
    payload["object_attributes"].update(note="@agent remediate", id=4100, position=None)
    client.post("/webhook", json=payload, headers=NOTE_HEADERS)
    platform.run_once()
    assert not fake.posted(r"/sandbox/pipeline")
    reply = str(fake.posted(r"/merge_requests/5/discussions"))
    assert "no security findings at or above `high` in pipeline #601" in reply
