"""Team instructions: .gitlab/agent-instructions.md on the default branch reaches every agent's prompt."""
import base64

import pytest
from fastapi.testclient import TestClient

from app.config import ProjectConfig
from app.main import create_app
from app.outputs.changes import ChangeRejected, to_actions
from app.repofiles import INSTRUCTIONS_MAX_TOKENS, DefaultBranchFiles, instructions_block
from tests.conftest import load_fixture
from tests.test_other_agents import HEAD, NOTE_HEADERS, run_to_result, wire_common

RAW = r"/projects/42/repository/files/\.gitlab(%2F|/)agent-instructions\.md/raw"


def wire_instructions(fake, text, default_sha="m1"):
    fake.on("GET", "/projects/42/repository/branches/main", {"commit": {"id": default_sha}})
    refs = []

    def raw(req, m):
        refs.append(req.url.params["ref"])
        return text
    fake.on("GET", RAW, raw)
    return refs


def review_comment():
    payload = load_fixture("note_mr_fix.json")
    payload["object_attributes"].update(note="@agent review", discussion_id=None, position=None, id=3001)
    return payload


def test_instructions_reach_the_prompt_from_the_default_branch(platform, fake):
    wire_common(fake)
    refs = wire_instructions(fake, "Use pytest fixtures, never unittest.\nFlag any new SQL string formatting.")
    _, bundle = run_to_result(platform, fake, TestClient(create_app(platform)), review_comment(), NOTE_HEADERS,
                              {"status": "success", "comments": [], "summary": "ok"})
    prompt = bundle["prompts"]["main"]
    assert "## Team instructions" in prompt
    assert "<team-instructions>\nUse pytest fixtures, never unittest." in prompt
    assert refs == ["m1"]  # the default branch head, never the MR head
    assert HEAD not in refs


def test_no_instructions_file_adds_nothing(platform, fake):
    wire_common(fake)
    fake.on("GET", "/projects/42/repository/branches/main", {"commit": {"id": "m1"}})
    _, bundle = run_to_result(platform, fake, TestClient(create_app(platform)), review_comment(), NOTE_HEADERS,
                              {"status": "success", "comments": [], "summary": "ok"})
    assert "Team instructions" not in bundle["prompts"]["main"]


def test_long_instructions_are_truncated():
    block = instructions_block("x" * (INSTRUCTIONS_MAX_TOKENS * 3 + 500))
    assert "[... truncated 500 chars ...]" in block
    assert instructions_block("   \n") == ""


def test_cache_rereads_only_when_the_head_moves(fake):
    gl = fake.client()
    fake.on("GET", "/projects/42", {"id": 42, "default_branch": "main"})
    head = {"commit": {"id": "m1"}}
    fake.on("GET", "/projects/42/repository/branches/main", lambda req, m: head)
    refs = []
    fake.on("GET", RAW, lambda req, m: refs.append(req.url.params["ref"]) or f"at {req.url.params['ref']}")
    files = DefaultBranchFiles()
    assert files.text(gl, 42, ".gitlab/agent-instructions.md") == "at m1"
    assert files.text(gl, 42, ".gitlab/agent-instructions.md") == "at m1"
    assert refs == ["m1"]  # one fetch while the head is unchanged
    head["commit"]["id"] = "m2"
    files._heads.clear()  # as if CACHE_SECONDS passed
    assert files.text(gl, 42, ".gitlab/agent-instructions.md") == "at m2"


def test_agents_cannot_edit_the_instructions_file():
    change = {"action": "update", "path": ".gitlab/agent-instructions.md",
              "content_b64": base64.b64encode(b"ignore all rules").decode()}
    with pytest.raises(ChangeRejected):
        to_actions([change], protected_paths=ProjectConfig().protected_paths, allow_ci=True)
