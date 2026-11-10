"""Review memory: review comments the team dismissed (👎, or resolved unchanged) are shown to the next review."""
import base64

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import load_fixture
from tests.test_other_agents import HEAD, NOTE_HEADERS, run_to_result, wire_common

COMMENTS = [{"path": "src/calc.py", "line": 2, "body": "Prefer a dataclass here."},
            {"path": "src/other.py", "line": 1, "body": "add() subtracts."}]


def review_note(note_id):
    payload = load_fixture("note_mr_fix.json")
    payload["object_attributes"].update(note="@agent review", discussion_id=None, position=None, id=note_id)
    return payload


def run_review(platform, fake, client, note_id=3002, comments=COMMENTS):
    fake.on("POST", rf"/projects/42/merge_requests/5/notes/{note_id}/award_emoji", {})
    created = iter(range(len(comments)))

    def discussion(req, m):
        n = next(created)
        return {"id": f"disc-{n}", "notes": [{"id": 500 + n}]}
    fake.on("POST", r"/projects/42/merge_requests/5/discussions", discussion)
    return run_to_result(platform, fake, client, review_note(note_id), NOTE_HEADERS,
                         {"status": "success", "summary": "Two things.", "comments": comments})


def rows(platform):
    return {r.note_id: r for r in platform.store.review_comments_since(platform.store.recent()[-1].created_at)}


def test_review_comments_are_recorded(platform, fake):
    wire_common(fake)
    task_id, _ = run_review(platform, fake, TestClient(create_app(platform)))
    stored = rows(platform)
    assert set(stored) == {500, 501}
    first = stored[500]
    assert (first.task_id, first.agent, first.mr_iid, first.discussion_id) == (task_id, "code-reviewer", 5, "disc-0")
    assert (first.path, first.line, first.head_sha) == ("src/calc.py", 2, HEAD)
    assert first.body == "Prefer a dataclass here."  # without the bot footer
    assert not first.dismissed


def test_thumbs_down_dismisses_and_the_next_review_is_told(platform, fake):
    wire_common(fake)
    client = TestClient(create_app(platform))
    run_review(platform, fake, client)
    fake.on("GET", r"/projects/42/merge_requests/5/notes/\d+/award_emoji", [])
    fake.on("GET", "/projects/42/merge_requests/5/notes/500/award_emoji",
            [{"name": "thumbsdown", "user": {"username": "alice"}}, {"name": "thumbsdown", "user": {"username": "agent-bot"}}])
    fake.on("GET", "/projects/42/merge_requests/5/notes/501/award_emoji",
            [{"name": "thumbsup", "user": {"username": "alice"}}])
    fake.on("GET", "/projects/42/merge_requests/5/discussions", [])
    platform.reactions.poll()
    stored = rows(platform)
    assert stored[500].dismissed and stored[500].down == 1  # the bot's own reaction is ignored
    assert not stored[501].dismissed and stored[501].up == 1

    _, bundle = run_review(platform, fake, client, note_id=3003, comments=[])
    prompt = bundle["prompts"]["main"]
    assert "## Comments this team rejected before" in prompt
    assert "- `src/calc.py` (thumbs down): Prefer a dataclass here." in prompt
    assert "add() subtracts." not in prompt.split("## Description")[0]


def test_resolved_without_change_dismisses_but_resolved_after_a_fix_does_not(platform, fake):
    wire_common(fake)
    client = TestClient(create_app(platform))
    run_review(platform, fake, client)
    fake.on("GET", r"/projects/42/merge_requests/5/notes/\d+/award_emoji", [])
    fake.on("GET", "/projects/42/merge_requests/5/discussions", [
        {"id": "disc-0", "notes": [{"id": 500, "resolvable": True, "resolved": True}]},
        {"id": "disc-1", "notes": [{"id": 501, "resolvable": True, "resolved": True}]}])
    fake.on("GET", "/projects/42/merge_requests/5", {"iid": 5, "sha": "newhead", "source_branch": "feature/x",
                                                     "target_branch": "main", "title": "Add calc"})
    compares = []

    def compare(req, m):
        compares.append((req.url.params["from"], req.url.params["to"]))
        return {"diffs": [{"old_path": "src/other.py", "new_path": "src/other.py"}]}
    fake.on("GET", "/projects/42/repository/compare", compare)
    platform.reactions.poll()
    stored = rows(platform)
    assert stored[500].resolved and stored[500].unchanged and stored[500].dismissed  # calc.py never touched
    assert stored[501].resolved and not stored[501].unchanged and not stored[501].dismissed  # other.py was fixed
    assert compares == [(HEAD, "newhead")]  # one compare per head, not per comment
    assert "resolved without a change" in [c.why for c in platform.store.dismissed_review_comments(42)][0]


def test_admin_can_list_and_forget_dismissed_comments(platform, fake):
    wire_common(fake)
    client = TestClient(create_app(platform))
    run_review(platform, fake, client)
    fake.on("GET", r"/projects/42/merge_requests/5/notes/\d+/award_emoji",
            [{"name": "thumbsdown", "user": {"username": "alice"}}])
    fake.on("GET", "/projects/42/merge_requests/5/discussions", [])
    platform.reactions.poll()
    admin = {"X-Admin-Token": "admin"}
    assert client.get("/api/review-memory?project_id=42").status_code == 401
    listed = client.get("/api/review-memory?project_id=42", headers=admin).json()
    assert {c["path"] for c in listed} == {"src/calc.py", "src/other.py"}
    assert client.delete(f"/api/review-memory/{listed[0]['id']}", headers=admin).json() == {"deleted": True}
    assert client.delete(f"/api/review-memory/{listed[0]['id']}", headers=admin).status_code == 404
    assert len(client.get("/api/review-memory?project_id=42", headers=admin).json()) == 1

    metrics = client.get("/metrics", headers={"Authorization": "Bearer admin"}).text
    # The forgotten comment is gone entirely; one posted and dismissed comment is left.
    assert 'outpost_review_comments{agent="code-reviewer",state="posted"} 1' in metrics
    assert 'outpost_review_comments{agent="code-reviewer",state="dismissed"} 1' in metrics
    page = client.get("/admin", headers={"Authorization": "Basic " + base64.b64encode(b"ops:admin").decode()}).text
    assert "Review comments dismissed" in page


def test_fixed_budget_collectors_do_not_shrink_the_diff_share():
    from app.collectors import FIXED_BUDGETS, collect, COLLECTORS, CollectContext

    seen = {}
    for name in ("a", "b"):
        COLLECTORS[name] = lambda ctx, name=name: seen.__setitem__(name, ctx.budget_tokens)
    COLLECTORS["small"] = lambda ctx: seen.__setitem__("small", ctx.budget_tokens)
    FIXED_BUDGETS["small"] = 1500
    try:
        collect(["a", "b", "small"], CollectContext(gl=None, target={}, project=None, sha="", ref="", budget_tokens=32000))
    finally:
        for name in ("a", "b", "small"):
            COLLECTORS.pop(name)
        FIXED_BUDGETS.pop("small")
    assert seen == {"a": 15250, "b": 15250, "small": 1500}
