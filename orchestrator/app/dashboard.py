"""Operator views: an HTML dashboard (/admin) and Prometheus metrics (/metrics). No JavaScript, no CDN."""
from __future__ import annotations

import base64
import hmac
from collections import Counter, defaultdict
from datetime import timedelta
from statistics import median
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from jinja2 import Environment

from .tasks import (ACTIVE, DISPATCHED, DONE, FAILED, MR_CLOSED, MR_MERGED, PIPELINE_GREEN, QUEUED,
                    THUMBS_DOWN, THUMBS_UP, Task, utcnow)

if TYPE_CHECKING:
    from .platform import Platform

WINDOWS = (7, 30, 90)


def admin_checker(token: str | None):  # noqa: ANN201
    """FastAPI dependency: X-Admin-Token, `Authorization: Bearer` (Prometheus) or HTTP Basic (browsers)."""

    def check(request: Request) -> None:
        offered = request.headers.get("x-admin-token", "")
        auth = request.headers.get("authorization", "")
        scheme, _, value = auth.partition(" ")
        if not offered and scheme.lower() == "bearer":
            offered = value.strip()
        elif not offered and scheme.lower() == "basic":
            try:
                offered = base64.b64decode(value.strip()).decode("utf-8").partition(":")[2]
            except ValueError:
                offered = ""
        if not token or not offered or not hmac.compare_digest(offered, token):
            raise HTTPException(status_code=401, detail="admin token required",
                                headers={"WWW-Authenticate": 'Basic realm="outpost"'})

    return check


# -- aggregation ---------------------------------------------------------------------------

def _duration(t: Task) -> float | None:
    if t.status not in (DONE, FAILED) or not t.updated_at or not t.created_at:
        return None
    return max((t.updated_at - t.created_at).total_seconds(), 0.0)


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    idx = min(int(round(q * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[idx]


def compute(platform: "Platform", days: int, project: str | None = None) -> dict[str, Any]:
    """Per-agent statistics over the last `days` days."""
    store = platform.store
    tasks = store.tasks_since(utcnow() - timedelta(days=days), project or None)
    fb = store.feedback_for([t.id for t in tasks])
    kinds_by_task: dict[str, set[str]] = defaultdict(set)
    reactions: dict[str, Counter[str]] = defaultdict(Counter)
    for f in fb:
        kinds_by_task[f.task_id].add(f.kind)
        if f.kind in (THUMBS_UP, THUMBS_DOWN):
            reactions[f.task_id][f.kind] += 1

    agents: dict[str, dict[str, Any]] = {}
    durations: dict[str, list[float]] = defaultdict(list)
    for t in tasks:
        a = agents.setdefault(t.agent, {"agent": t.agent, "total": 0, "done": 0, "failed": 0, "active": 0,
                                        "merged": 0, "closed": 0, "green": 0, "up": 0, "down": 0, "tool_calls": 0})
        a["total"] += 1
        a["tool_calls"] += t.tool_calls or 0
        if t.status == DONE:
            a["done"] += 1
        elif t.status == FAILED:
            a["failed"] += 1
        elif t.status in ACTIVE:
            a["active"] += 1
        kinds = kinds_by_task.get(t.id, set())
        if MR_MERGED in kinds:
            a["merged"] += 1
        elif MR_CLOSED in kinds:
            a["closed"] += 1
        if PIPELINE_GREEN in kinds:
            a["green"] += 1
        a["up"] += reactions[t.id][THUMBS_UP]
        a["down"] += reactions[t.id][THUMBS_DOWN]
        d = _duration(t)
        if d is not None:
            durations[t.agent].append(d)
    for name, a in agents.items():
        decided = a["merged"] + a["closed"]
        a["acceptance"] = round(100 * a["merged"] / decided) if decided else None
        a["median_seconds"] = round(median(durations[name])) if durations[name] else None
        a["avg_tool_calls"] = round(a["tool_calls"] / a["total"], 1) if a["total"] else 0
    return {
        "agents": sorted(agents.values(), key=lambda a: (-a["total"], a["agent"])),
        "durations": durations,
        "tasks": tasks,
        "reactions": reactions,
        "kinds": kinds_by_task,
    }


def queue_depth(platform: "Platform") -> dict[str, int]:
    lanes = {"inline": 0, "sandbox": 0}
    for t in platform.store.list_by_status(QUEUED, DISPATCHED):
        lanes["inline" if t.agent in platform.inline_agents else "sandbox"] += 1
    return lanes


# -- Prometheus ----------------------------------------------------------------------------

def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def metrics_text(platform: "Platform", days: int = 30) -> str:
    stats = compute(platform, days)
    lines = [
        f"# HELP outpost_tasks Tasks created in the last {days} days, by agent and status.",
        "# TYPE outpost_tasks gauge",
    ]
    by_status: Counter[tuple[str, str]] = Counter((t.agent, t.status) for t in stats["tasks"])
    for (agent, status), n in sorted(by_status.items()):
        lines.append(f'outpost_tasks{{agent="{_label(agent)}",status="{_label(status)}"}} {n}')
    lines += [f"# HELP outpost_feedback Feedback on tasks from the last {days} days, by agent and kind.",
              "# TYPE outpost_feedback gauge"]
    fields = {"thumbs_up": "up", "thumbs_down": "down", "mr_merged": "merged", "mr_closed": "closed",
              "pipeline_green": "green"}
    for a in stats["agents"]:
        for kind, key in fields.items():
            lines.append(f'outpost_feedback{{agent="{_label(a["agent"])}",kind="{kind}"}} {a[key]}')
    lines += ["# HELP outpost_task_duration_seconds Time from event to finished task.",
              "# TYPE outpost_task_duration_seconds summary"]
    for agent, values in sorted(stats["durations"].items()):
        for q in (0.5, 0.9, 0.99):
            lines.append(f'outpost_task_duration_seconds{{agent="{_label(agent)}",quantile="{q}"}} '
                         f'{_quantile(values, q):.0f}')
        lines.append(f'outpost_task_duration_seconds_sum{{agent="{_label(agent)}"}} {sum(values):.0f}')
        lines.append(f'outpost_task_duration_seconds_count{{agent="{_label(agent)}"}} {len(values)}')
    lines += ["# HELP outpost_tool_calls GitLab tool calls made by tasks from the window.",
              "# TYPE outpost_tool_calls gauge"]
    for a in stats["agents"]:
        lines.append(f'outpost_tool_calls{{agent="{_label(a["agent"])}"}} {a["tool_calls"]}')
    lines += ["# HELP outpost_queue_depth Tasks queued or running, by lane.", "# TYPE outpost_queue_depth gauge"]
    for lane, n in queue_depth(platform).items():
        lines.append(f'outpost_queue_depth{{lane="{lane}"}} {n}')
    return "\n".join(lines) + "\n"


# -- HTML ----------------------------------------------------------------------------------

TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Outpost dashboard</title>
<style>
:root{--bg:#f7f7f8;--fg:#1d1d20;--muted:#6b6b76;--card:#fff;--line:#e3e3e8;--good:#1a7f37;--bad:#cf222e;--accent:#5b4bdb}
@media (prefers-color-scheme:dark){:root{--bg:#141417;--fg:#e8e8ec;--muted:#9a9aa6;--card:#1d1d22;--line:#2e2e36;
--good:#3fb950;--bad:#f85149;--accent:#9d90ff}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1200px;margin:0 auto;padding:24px 16px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:28px 0 10px}
.muted{color:var(--muted)}a{color:var(--accent)}
form{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}
select,button{font:inherit;padding:4px 8px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px}
.tile b{display:block;font-size:22px}
.scroll{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%}th,td{padding:7px 10px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-weight:600;color:var(--muted);font-size:12px}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.good{color:var(--good)}.bad{color:var(--bad)}td.wrap{white-space:normal;max-width:420px;overflow-wrap:anywhere}
</style></head><body><main>
<h1>Outpost</h1><div class="muted">Agent activity and feedback · last {{ days }} days{% if project %} · {{ project }}{% endif %}</div>
<form method="get">
<select name="days">{% for d in windows %}<option value="{{ d }}"{% if d == days %} selected{% endif %}>{{ d }} days</option>{% endfor %}</select>
<select name="project"><option value="">All projects</option>{% for p in projects %}<option{% if p == project %} selected{% endif %}>{{ p }}</option>{% endfor %}</select>
<button type="submit">Apply</button></form>
<div class="tiles">
<div class="tile"><span class="muted">Tasks</span><b>{{ totals.total }}</b></div>
<div class="tile"><span class="muted">Failed</span><b class="{{ 'bad' if totals.failed else '' }}">{{ totals.failed }}</b></div>
<div class="tile"><span class="muted">MR acceptance</span><b>{{ totals.acceptance if totals.acceptance is not none else '–' }}{% if totals.acceptance is not none %}%{% endif %}</b></div>
<div class="tile"><span class="muted">👍 / 👎</span><b>{{ totals.up }} / {{ totals.down }}</b></div>
<div class="tile"><span class="muted">Queued · inline / sandbox</span><b>{{ queue.inline }} / {{ queue.sandbox }}</b></div>
</div>
<h2>Agents</h2>
<div class="scroll"><table><tr><th>Agent</th><th class="num">Tasks</th><th class="num">Done</th><th class="num">Failed</th>
<th class="num">Running</th><th class="num">MRs merged / closed</th><th class="num">Acceptance</th><th class="num">Green pipelines</th>
<th class="num">👍</th><th class="num">👎</th><th class="num">Median time</th><th class="num">Avg tool calls</th></tr>
{% for a in agents %}<tr><td>{{ a.agent }}</td><td class="num">{{ a.total }}</td><td class="num">{{ a.done }}</td>
<td class="num {{ 'bad' if a.failed else '' }}">{{ a.failed }}</td><td class="num">{{ a.active }}</td>
<td class="num">{{ a.merged }} / {{ a.closed }}</td><td class="num">{{ a.acceptance ~ '%' if a.acceptance is not none else '–' }}</td>
<td class="num">{{ a.green }}</td><td class="num good">{{ a.up }}</td><td class="num bad">{{ a.down }}</td>
<td class="num">{{ a.median_seconds|duration }}</td><td class="num">{{ a.avg_tool_calls }}</td></tr>
{% else %}<tr><td colspan="12" class="muted">No tasks in this window.</td></tr>{% endfor %}</table></div>
{% if flows %}<h2>Running flows</h2>
<div class="scroll"><table><tr><th>Flow</th><th>Project</th><th>Status</th><th class="num">Step</th><th>Started</th></tr>
{% for f in flows %}<tr><td>{{ f.flow }}</td><td>{{ f.project_path }}</td><td>{{ f.status }}</td><td class="num">{{ f.step + 1 }}</td>
<td>{{ f.created_at.strftime('%Y-%m-%d %H:%M') }}</td></tr>{% endfor %}</table></div>{% endif %}
<h2>Recent tasks</h2>
<div class="scroll"><table><tr><th>Created (UTC)</th><th>Agent</th><th>Project</th><th>Status</th><th>Feedback</th><th>Outcome</th><th></th></tr>
{% for r in recent %}<tr><td>{{ r.task.created_at.strftime('%Y-%m-%d %H:%M') }}</td><td>{{ r.task.agent }}</td><td>{{ r.task.project_path }}</td>
<td class="{{ 'bad' if r.task.status == 'failed' else '' }}">{{ r.task.status }}</td><td>{{ r.badges }}</td>
<td class="wrap">{% if r.link %}<a href="{{ r.link }}">{{ r.link }}</a>{% else %}{{ r.text }}{% endif %}</td>
<td>{% if r.exportable %}<a href="api/tasks/{{ r.task.id }}/fixture" title="Download as an eval fixture">Export fixture</a>{% endif %}</td></tr>
{% endfor %}</table></div>
<p class="muted">Prometheus metrics: <code>/metrics</code> (bearer admin token).</p>
</main></body></html>
"""


def _duration_filter(seconds: Any) -> str:
    if seconds is None:
        return "–"
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    return f"{seconds / 3600:.1f}h"


_env = Environment(autoescape=True)
_env.filters["duration"] = _duration_filter
_template = _env.from_string(TEMPLATE)


def render(platform: "Platform", days: int, project: str | None) -> str:
    stats = compute(platform, days, project)
    agents = stats["agents"]
    totals = {k: sum(a[k] for a in agents) for k in ("total", "failed", "merged", "closed", "up", "down")}
    decided = totals["merged"] + totals["closed"]
    totals["acceptance"] = round(100 * totals["merged"] / decided) if decided else None
    recent = []
    for t in stats["tasks"][:100]:
        r = stats["reactions"][t.id]
        kinds = stats["kinds"].get(t.id, set())
        badges = " ".join(filter(None, [
            f"👍{r[THUMBS_UP]}" if r[THUMBS_UP] else "", f"👎{r[THUMBS_DOWN]}" if r[THUMBS_DOWN] else "",
            "merged" if MR_MERGED in kinds else ("closed" if MR_CLOSED in kinds else ""),
            "green" if PIPELINE_GREEN in kinds else "",
        ]))
        outcome = t.outcome or t.error or ""
        recent.append({"task": t, "badges": badges,
                       "link": outcome if outcome.startswith(("http://", "https://")) else "",
                       "text": outcome[:200],
                       "exportable": t.bundle_json is not None and (t.status == FAILED or r[THUMBS_DOWN] > 0
                                                                    or MR_CLOSED in kinds)})
    flows = platform.store.active_runs(project)
    return _template.render(days=days, project=project or "", windows=WINDOWS, projects=platform.store.project_paths(),
                            agents=agents, totals=totals, queue=queue_depth(platform), recent=recent, flows=flows)


def register(app: FastAPI, platform: "Platform") -> None:
    require_admin = admin_checker(platform.settings.admin_token)

    @app.get("/admin", response_class=HTMLResponse)
    def admin(request: Request, days: int = Query(30), project: str = Query("")) -> HTMLResponse:
        require_admin(request)
        return HTMLResponse(render(platform, days if days in WINDOWS else 30, project or None))

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics(request: Request) -> PlainTextResponse:
        require_admin(request)
        return PlainTextResponse(metrics_text(platform), media_type="text/plain; version=0.0.4")
