"""Repository-defined agents: validation against the admin policy, loading from the default branch, running."""
import yaml
from fastapi.testclient import TestClient

from app.catalog import parse
from app.config import CustomAgentsPolicy, ProjectsConfig
from app.main import create_app
from tests.conftest import load_fixture
from tests.test_other_agents import NOTE_HEADERS, run_to_result, wire_common

CHANGELOG = {
    "name": "changelog",
    "description": "Drafts a changelog entry for a merge request.",
    "prompt": "Write a changelog entry for MR !{{ context.mr.iid }}: {{ context.mr.title }}. Submit it as a note.",
    "context": ["mr", "mr_diff"],
    "tools": ["get_merge_request"],
    "triggers": [{"event": "note", "commands": ["changelog"], "targets": ["merge_request"]}],
}


def files(*agents):
    return {f".gitlab/agents/{a['name']}.yaml": yaml.safe_dump(a) for a in agents}


def test_valid_agent_is_namespaced(registry):
    catalog = parse(files(CHANGELOG), CustomAgentsPolicy(enabled=True), registry, "sha")
    assert catalog.errors == {}
    agent = catalog.agents[0]
    assert agent.name == "custom/changelog" and agent.source == "repo" and agent.commands == ["changelog"]


def test_shipped_example_is_valid(registry):
    from tests.conftest import ROOT
    examples = {str(p): p.read_text(encoding="utf-8") for p in (ROOT / "deploy" / "repo-example" / "agents").glob("*.yaml")}
    catalog = parse(examples, CustomAgentsPolicy(enabled=True), registry, "sha")
    assert examples and catalog.errors == {} and len(catalog.agents) == len(examples)


def test_policy_violations_are_skipped_with_reasons(registry):
    bad = {
        "edit": {**CHANGELOG, "name": "edit", "mode": "edit", "engine": "aider", "output": "mr_commit",
                 "triggers": [{"event": "note", "commands": ["e"], "targets": ["merge_request"]}]},
        "clash": {**CHANGELOG, "name": "clash", "triggers": [{"event": "note", "commands": ["review"]}]},
        "pipeline": {**CHANGELOG, "name": "pipeline", "triggers": [{"event": "pipeline", "status": "failed"}]},
        "marker": {**CHANGELOG, "name": "marker", "note_marker": "<!-- agent:plan -->"},
        "tool": {**CHANGELOG, "name": "tool", "tools": ["get_file"]},
        "label": {**CHANGELOG, "name": "label", "triggers": [{"event": "label", "label": "agent::review"}]},
        "ci": {**CHANGELOG, "name": "ci", "context": ["failed_jobs"]},
    }
    catalog = parse(files(*bad.values()), CustomAgentsPolicy(enabled=True), registry, "sha")
    assert catalog.agents == []
    reasons = {path.rsplit("/", 1)[1].split(".")[0]: why for path, why in catalog.errors.items()}
    assert set(reasons) == set(bad)
    assert "edit agents are not allowed" in reasons["edit"]
    assert "review" in reasons["clash"]
    assert "get_file" in reasons["tool"]
    assert "agent::review" in reasons["label"]


def test_two_custom_agents_cannot_share_a_command(registry):
    other = {**CHANGELOG, "name": "changelog2"}
    catalog = parse(files(CHANGELOG, other), CustomAgentsPolicy(enabled=True), registry, "sha")
    assert len(catalog.agents) == 1 and "already used" in next(iter(catalog.errors.values()))


def enable_custom_agents(platform, fake, agent_files):
    platform.projects = ProjectsConfig(
        defaults={"sandbox_image": "registry.internal/agent/sandbox-python:1"},
        allowed_image_prefixes=["registry.internal/"],
        projects={"group/app": {"agents": ["code-reviewer"], "custom_agents": {"enabled": True}}},
    )
    fake.on("GET", "/projects/42/repository/branches/main", {"commit": {"id": "m1"}})
    fake.on("GET", "/projects/42/repository/tree",
            [{"type": "blob", "path": p} for p in agent_files] + [{"type": "blob", "path": ".gitlab/agents/README.md"}])
    for path, text in agent_files.items():
        fake.on("GET", "/projects/42/repository/files/" + path.replace("/", "(%2F|/)").replace(".", r"\.") + "/raw", text)


def changelog_comment(note="@agent changelog please"):
    payload = load_fixture("note_mr_fix.json")
    payload["object_attributes"].update(note=note, discussion_id=None, position=None, id=3005)
    return payload


def test_custom_agent_runs_end_to_end(platform, fake):
    wire_common(fake)
    enable_custom_agents(platform, fake, files(CHANGELOG))
    fake.on("POST", r"/projects/42/merge_requests/5/notes/3005/award_emoji", {})
    client = TestClient(create_app(platform))
    task_id, bundle = run_to_result(platform, fake, client, changelog_comment(), NOTE_HEADERS,
                                    {"status": "success", "body": "### Changed\n- calc"})
    assert bundle["agent"]["name"] == "custom/changelog"
    prompt = bundle["prompts"]["main"]
    assert prompt.startswith("Content inside <untrusted-data>")  # preamble is always added
    assert "MR !5: Add calc" in prompt
    assert [t["name"] for t in bundle["tools"]] == ["get_merge_request"]
    assert "### Changed" in fake.posted(r"/merge_requests/5/notes$")[-1]["body"]
    # The snapshot keeps the task runnable even if the repo file changes later.
    assert platform.store.get(task_id).agent_json and platform.agent_for(platform.store.get(task_id)).source == "repo"


def test_custom_agents_listed_in_help_with_skipped_files(platform, fake):
    wire_common(fake)
    broken = {".gitlab/agents/broken.yaml": "name: Broken Name\n"}
    enable_custom_agents(platform, fake, {**files(CHANGELOG), **broken})
    TestClient(create_app(platform)).post("/webhook", json=changelog_comment("@agent help"), headers=NOTE_HEADERS)
    reply = fake.posted(r"/merge_requests/5/notes$")[-1]["body"]
    assert "`custom/changelog`" in reply and "`@agent changelog`" in reply
    assert "`.gitlab/agents/broken.yaml`" in reply


def test_custom_agents_off_by_default(platform, fake):
    wire_common(fake)
    TestClient(create_app(platform)).post("/webhook", json=changelog_comment(), headers=NOTE_HEADERS)
    assert not any("/repository/tree" in path for _, path, _ in fake.calls)
    assert "I don't know the command `changelog`" in fake.posted(r"/merge_requests/5/notes$")[-1]["body"]
