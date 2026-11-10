import base64

import pytest

from app import events, tickets
from app.collectors.ci import focus_trace
from app.config import ProjectConfig
from app.outputs.changes import ChangeRejected, to_actions
from app.outputs.common import sanitize
from app.router import parse_mention, route
from tests.conftest import load_fixture


def b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


# -- mentions / routing ----------------------------------------------------------------

@pytest.mark.parametrize("body,expected", [
    ("@agent fix this please", ("fix", "this please")),
    ("hey @agent review", ("review", "")),
    ("@agent", ("", "")),
    ("> @agent fix quoted\nnothing", None),
    ("```\n@agent fix in code\n```", None),
    ("email me at x@agent.example", None),
    ("`@agent fix` is how you call it", None),
])
def test_parse_mention(body, expected):
    assert parse_mention(body, "@agent") == expected


def test_pipeline_failure_routes_to_fixer(registry):
    ev = events.parse("Pipeline Hook", load_fixture("pipeline_failed.json"))
    assert ev.failed_job_ids == [9001]  # allow_failure jobs are ignored
    result = route(ev, registry, ProjectConfig(agents=["pipeline-fixer"]), "agent-bot", "@agent")
    assert [m.agent.name for m in result.matches] == ["pipeline-fixer"]
    assert not result.needs_permission_check


def test_pipeline_branch_filter(registry):
    ev = events.parse("Pipeline Hook", load_fixture("pipeline_failed.json"))
    cfg = ProjectConfig(agents=["pipeline-fixer"], pipeline_fixer={"branches": ["release/*"]})
    assert route(ev, registry, cfg, "agent-bot", "@agent").matches == []


def test_note_routes_to_mr_assist_with_instruction(registry):
    ev = events.parse("Note Hook", load_fixture("note_mr_fix.json"))
    result = route(ev, registry, ProjectConfig(agents=["mr-assist"]), "agent-bot", "@agent")
    assert result.matches[0].agent.name == "mr-assist"
    assert result.matches[0].instruction == "this please use <= here"
    assert result.needs_permission_check


def test_bot_comments_are_ignored(registry):
    payload = load_fixture("note_mr_fix.json")
    payload["user"]["username"] = "agent-bot"
    ev = events.parse("Note Hook", payload)
    assert route(ev, registry, ProjectConfig(agents=["mr-assist"]), "agent-bot", "@agent").matches == []


def test_unknown_or_disabled_command_replies_with_help(registry):
    ev = events.parse("Note Hook", load_fixture("note_mr_fix.json"))
    result = route(ev, registry, ProjectConfig(agents=["pipeline-fixer"]), "agent-bot", "@agent")
    assert result.matches == [] and "don't know the command `fix`" in result.reply


def test_label_trigger(registry):
    ev = events.parse("Issue Hook", load_fixture("issue_label_plan.json"))
    assert ev.added_labels == ["agent::plan"]
    result = route(ev, registry, ProjectConfig(agents=["planner", "code-writer"]), "agent-bot", "@agent")
    assert [m.agent.name for m in result.matches] == ["planner"]


def test_mr_update_without_new_commits_does_not_rereview(registry):
    payload = {
        "object_kind": "merge_request", "user": {"id": 7, "username": "alice"},
        "project": {"id": 42, "path_with_namespace": "group/app"},
        "object_attributes": {"iid": 5, "action": "update", "last_commit": {"id": "c1"}},
        "labels": [{"title": "agent::review"}],
    }
    cfg = ProjectConfig(agents=["code-reviewer"])
    assert route(events.parse("Merge Request Hook", payload), registry, cfg, "agent-bot", "@agent").matches == []
    payload["object_attributes"]["oldrev"] = "c0"
    assert len(route(events.parse("Merge Request Hook", payload), registry, cfg, "agent-bot", "@agent").matches) == 1


# -- tickets ----------------------------------------------------------------------------

def test_ticket_roundtrip_and_scope():
    t = tickets.issue("s", "task1", 60, now=1000)
    assert tickets.verify("s", t, "task1", now=1030)
    assert not tickets.verify("s", t, "task2", now=1030)  # bound to its task
    assert not tickets.verify("s", t, "task1", now=2000)  # expired
    assert not tickets.verify("other", t, "task1", now=1030)
    assert not tickets.verify("s", t.replace("task1", "task2"), "task2", now=1030)


# -- trace focusing -----------------------------------------------------------------------

def test_focus_trace_keeps_errors_and_tail():
    lines = ["\x1b[32;1mRunning with gitlab-runner 17.0\x1b[0;m"] + [f"noise line {i}" for i in range(2000)]
    lines[300] = "ERROR: could not import module foo"
    lines += ["FAILED tests/test_calc.py::test_add - assert 3 == 4", "Cleaning up project directory"]
    out = focus_trace("\n".join(lines), max_tokens=3000)
    assert "could not import module foo" in out
    assert "FAILED tests/test_calc.py::test_add" in out
    assert "Running with gitlab-runner" not in out and "\x1b" not in out
    assert "lines omitted" in out


# -- change validation ------------------------------------------------------------------

def test_to_actions_accepts_normal_changes():
    actions = to_actions([
        {"action": "update", "path": "src/a.py", "content_b64": b64("x = 1\n")},
        {"action": "delete", "path": "old.txt"},
        {"action": "move", "path": "new/b.py", "previous_path": "b.py", "content_b64": b64("y")},
    ], protected_paths=[".gitlab/agent.yml"], allow_ci=False)
    assert [a["action"] for a in actions] == ["update", "delete", "move"]
    assert actions[0]["encoding"] == "base64"


@pytest.mark.parametrize("change,allow_ci", [
    ({"action": "update", "path": "../etc/passwd", "content_b64": b64("x")}, False),
    ({"action": "update", "path": "/abs", "content_b64": b64("x")}, False),
    ({"action": "update", "path": ".git/config", "content_b64": b64("x")}, False),
    ({"action": "update", "path": ".gitlab/agent.yml", "content_b64": b64("x")}, True),
    ({"action": "update", "path": ".gitlab-ci.yml", "content_b64": b64("x")}, False),
    ({"action": "update", "path": ".gitlab/ci/deploy.yml", "content_b64": b64("x")}, False),
    ({"action": "create", "path": "a.py"}, False),
    ({"action": "chmod", "path": "a.py"}, False),
])
def test_to_actions_rejects_unsafe(change, allow_ci):
    with pytest.raises(ChangeRejected):
        to_actions([change], protected_paths=[".gitlab/agent.yml"], allow_ci=allow_ci)


def test_ci_changes_allowed_when_agent_permits():
    assert to_actions([{"action": "update", "path": ".gitlab-ci.yml", "content_b64": b64("a: 1")}], [], allow_ci=True)


def test_protected_directory_pattern():
    with pytest.raises(ChangeRejected):
        to_actions([{"action": "update", "path": "deploy/prod/values.yaml", "content_b64": b64("x")}], ["deploy/"], False)


# -- output sanitising ------------------------------------------------------------------

def test_sanitize_neutralises_quick_actions_and_mass_mentions():
    out = sanitize("Fixed it.\n/merge\n  /approve\nping @all")
    assert "\n/merge" not in out and "&#47;merge" in out
    assert "&#47;approve" in out
    assert "@all" not in out


# -- repo overrides ---------------------------------------------------------------------

def test_repo_overrides_can_only_narrow():
    base = ProjectConfig(agents=["pipeline-fixer", "planner"], sandbox_image="registry.internal/a:1",
                         protected_paths=[".gitlab/agent.yml"])
    merged = base.with_repo_overrides(
        {"agents": ["planner", "code-writer"], "sandbox_image": "docker.io/evil:latest",
         "protected_paths": ["migrations/"], "test": ["make test"], "custom_agents": {"enabled": True}},
        allowed_image_prefixes=["registry.internal/"],
    )
    assert merged.agents == ["planner"]  # code-writer was never enabled by the admin
    assert merged.sandbox_image == "registry.internal/a:1"  # untrusted registry ignored
    assert set(merged.protected_paths) == {".gitlab/agent.yml", ".gitlab/agents/",
                                         ".gitlab/agent-security-ignore.yml", ".gitlab/agent-instructions.md",
                                         "migrations/"}
    assert merged.test == ["make test"]
    assert merged.custom_agents.enabled is False  # a repo cannot turn custom agents on


def test_repo_can_turn_custom_agents_off():
    base = ProjectConfig(custom_agents={"enabled": True})
    assert base.with_repo_overrides({"custom_agents": False}, []).custom_agents.enabled is False


def test_agent_config_is_always_protected():
    assert {".gitlab/agent.yml", ".gitlab/agents/"} <= set(ProjectConfig(protected_paths=["deploy/"]).protected_paths)
