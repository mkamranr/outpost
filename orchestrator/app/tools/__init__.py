"""GitLab tools agents can call while they run.

Sandbox jobs hold no GitLab token, so they call these through the orchestrator (POST /api/tasks/{id}/tools/{name});
in-process agents such as chat call them directly. Every tool is read-only and scoped to the task's own project:
no argument can name another project. Results are untrusted data and are returned wrapped and truncated.
"""
from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from ..collectors import truncate
from ..gitlab_client import GitLab

RESULT_TOKENS = 3500  # ~10K chars per call: keeps a 30-turn loop inside a 27B model's useful window


@dataclass
class ToolContext:
    gl: GitLab
    project_id: int
    ref: str  # commit the task is about; the default for file reads
    bot_username: str = ""
    budget_tokens: int = RESULT_TOKENS


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    required: list[str]
    fn: Callable[..., Any]
    accepted: set[str] = field(default_factory=set)

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "parameters": {"type": "object", "properties": self.parameters, "required": self.required}}


TOOLS: dict[str, ToolSpec] = {}


def tool(name: str, description: str, parameters: dict[str, Any] | None = None,
         required: list[str] | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        accepted = set(inspect.signature(fn).parameters) - {"ctx"}
        TOOLS[name] = ToolSpec(name, description, parameters or {}, required or [], fn, accepted)
        return fn
    return wrap


def schemas(names: list[str]) -> list[dict[str, Any]]:
    return [TOOLS[n].schema() for n in names]


def _wrap(name: str, text: str) -> str:
    # The closing tag must not be forgeable from inside the data.
    text = text.replace("</untrusted-data", "&lt;/untrusted-data")
    return f'<untrusted-data source="tool:{name}">\n{text}\n</untrusted-data>'


def run(name: str, ctx: ToolContext, args: dict[str, Any]) -> str:
    """Call a tool and return text for the model. Errors are returned as text, never raised."""
    spec = TOOLS.get(name)
    if spec is None:
        return f"error: unknown tool {name}"
    if not isinstance(args, dict):
        return "error: arguments must be a JSON object"
    missing = [r for r in spec.required if r not in args]
    if missing:
        return f"error: missing argument(s): {', '.join(missing)}"
    try:
        value = spec.fn(ctx, **{k: v for k, v in args.items() if k in spec.accepted})
    except Exception as exc:  # noqa: BLE001 - report tool errors back to the model
        return f"error: {type(exc).__name__}: {str(exc)[:300]}"
    text = value if isinstance(value, str) else json.dumps(value, indent=1, default=str)
    return _wrap(name, truncate(text, ctx.budget_tokens, keep="head"))


from . import gitlab  # noqa: E402,F401  (register tools)
