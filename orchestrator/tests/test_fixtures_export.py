"""Exporting a finished task as an offline eval fixture."""
import base64
import importlib.util
import io
import tarfile

import httpx
import yaml
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import ROOT
from tests.test_platform_flow import SHA, dispatch_and_get_ticket, wire_pipeline_project

ADMIN = {"X-Admin-Token": "admin"}


def repo_archive() -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, data in [("app-x-x/src/calc.py", b"def add(a, b):\n    return a + b + 1\n"),
                           ("app-x-x/tests/test_calc.py", b"from src.calc import add\n"),
                           ("app-x-x/../evil.py", b"boom")]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("app-x-x/link")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tar.addfile(link)
    return out.getvalue()


def run_fix_with_tool_call(platform, fake, client):
    wire_pipeline_project(fake)
    task_id, ticket = dispatch_and_get_ticket(platform, fake, client)
    auth = {"Authorization": f"Bearer {ticket}"}
    resp = client.post(f"/api/tasks/{task_id}/tools/get_job_log", json={"job_id": 9001}, headers=auth)
    assert resp.status_code == 200
    result = {"status": "success", "classification": "code", "title": "Fix add()", "summary": "fixed",
              "changes": [{"action": "update", "path": "src/calc.py",
                           "content_b64": base64.b64encode(b"def add(a, b):\n    return a + b\n").decode()}]}
    client.post(f"/api/tasks/{task_id}/result", json=result, headers=auth)
    platform.run_once()
    return task_id


def unpack(data: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}


def load_eval_runner():
    spec = importlib.util.spec_from_file_location("eval_run", ROOT / "eval" / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_export_pipeline_fix_task(platform, fake, registry):
    client = TestClient(create_app(platform))
    task_id = run_fix_with_tool_call(platform, fake, client)
    assert platform.store.get(task_id).tool_log[0]["name"] == "get_job_log"
    fake.on("GET", "/projects/42/repository/archive.tar.gz",
            lambda req, m: httpx.Response(200, content=repo_archive()) if req.url.params["sha"] == SHA
            else httpx.Response(404))

    assert client.get(f"/api/tasks/{task_id}/fixture").status_code == 401
    resp = client.get(f"/api/tasks/{task_id}/fixture?name=Off By One!", headers=ADMIN)
    assert resp.status_code == 200 and resp.headers["x-fixture-repo"] == "full"
    files = unpack(resp.content)
    assert sorted(files) == ["off-by-one/case.yaml", "off-by-one/repo/src/calc.py", "off-by-one/repo/tests/test_calc.py"]

    case = yaml.safe_load(files["off-by-one/case.yaml"])
    assert case["draft"] is True and case["agent"] == "pipeline-fixer"
    assert case["expect"] == {"classification": "code", "tests_pass": True}
    assert case["test"] == ["pytest -q"]
    assert "FAILED tests/test_calc.py::test_add" in str(case["context"]["failed_jobs"])
    assert "FAILED tests/test_calc.py::test_add" in case["tool_responses"]["get_job_log"]
    assert case["source"]["task"] == task_id

    # The exported case renders through the eval harness unchanged.
    bundle = load_eval_runner().build_bundle(registry, case, "off-by-one", "f" * 40)
    assert "FAILED tests/test_calc.py::test_add" in bundle["prompts"]["main"]


def test_large_repo_exports_only_touched_files(platform, fake, settings):
    client = TestClient(create_app(platform))
    task_id = run_fix_with_tool_call(platform, fake, client)
    platform.settings.fixture_max_repo_mb = 0  # every archive is "too large"
    fake.on("GET", "/projects/42/repository/archive.tar.gz", lambda req, m: httpx.Response(200, content=repo_archive()))
    fake.on("GET", r"/projects/42/repository/files/src(%2F|/)calc\.py/raw", "def add(a, b):\n    return a + b + 1\n")
    resp = client.get(f"/api/tasks/{task_id}/fixture", headers=ADMIN)
    assert resp.headers["x-fixture-repo"] == "partial"
    name = f"pipeline-fixer-{task_id[:8]}"
    files = unpack(resp.content)
    assert sorted(files) == [f"{name}/case.yaml", f"{name}/repo/src/calc.py"]
    assert b"only the files the task touched" in files[f"{name}/case.yaml"]


def test_skipped_task_cannot_be_exported(platform):
    task = platform.store.create(agent="planner", project_id=42, project_path="group/app", dedupe_key="k",
                                 subject_key="s", target={"kind": "note", "project_id": 42})
    resp = TestClient(create_app(platform)).get(f"/api/tasks/{task.id}/fixture", headers=ADMIN)
    assert resp.status_code == 409


def test_eval_runner_skips_drafts():
    source = (ROOT / "eval" / "run.py").read_text(encoding="utf-8")
    assert "--include-drafts" in source and 'case.get("draft")' in source


def test_export_cli_extracts_safely(tmp_path):
    spec = importlib.util.spec_from_file_location("export_task", ROOT / "eval" / "export_task.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name in ("case-a/case.yaml", "case-a/repo/x.py", "case-a/../../escape.py", "/abs.py"):
            info = tarfile.TarInfo(name)
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
    assert module.safe_extract(out.getvalue(), tmp_path) == "case-a"
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()) == [
        "case-a/case.yaml", "case-a/repo/x.py"]


def test_every_eval_fixture_renders(registry):
    """A typo in a case or a prompt variable it lacks should fail here, not halfway through an eval run."""
    runner = load_eval_runner()
    for case_file in sorted((ROOT / "eval" / "fixtures").glob("*/case.yaml")):
        case = yaml.safe_load(case_file.read_text(encoding="utf-8"))
        bundle = runner.build_bundle(registry, case, case_file.parent.name, "0" * 40)
        assert bundle["prompts"]["main"].strip(), case_file
