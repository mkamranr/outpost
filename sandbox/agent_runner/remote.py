"""Talking to the orchestrator: the bundle/result exchange and GitLab tool calls, authenticated by the task ticket."""
from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Callable


def _ssl_context() -> ssl.SSLContext | None:
    ca = os.environ.get("AGENT_ORCHESTRATOR_CA_FILE") or os.environ.get("CI_SERVER_TLS_CA_FILE")
    return ssl.create_default_context(cafile=ca) if ca else None


def call(method: str, url: str, ticket: str, body: dict[str, Any] | None = None, attempts: int = 5) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {ticket}", "Content-Type": "application/json"})
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=60, context=_ssl_context()) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                raise RuntimeError(f"{method} {url} -> {exc.code}: {exc.read()[:300]!r}") from exc
            last = exc
        except OSError as exc:
            last = exc
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"{method} {url} failed: {last}")


class RemoteTools:
    """GitLab tools executed by something outside the agent: the orchestrator over HTTP, or a local function.

    `schemas` are the tool definitions from the bundle; `invoke(name, args) -> str` does the call.
    """

    def __init__(self, schemas: list[dict[str, Any]], invoke: Callable[[str, dict[str, Any]], str]):
        self.schemas = schemas
        self.names = {s["name"] for s in schemas}
        self._invoke = invoke

    def call(self, name: str, args: dict[str, Any]) -> str:
        if name not in self.names:
            return f"error: unknown tool {name}"
        try:
            return self._invoke(name, args)
        except Exception as exc:  # noqa: BLE001 - report tool errors back to the model
            return f"error: {exc}"[:500]

    @classmethod
    def http(cls, schemas: list[dict[str, Any]], orchestrator_url: str, task_id: str, ticket: str) -> "RemoteTools":
        def invoke(name: str, args: dict[str, Any]) -> str:
            return str(call("POST", f"{orchestrator_url}/api/tasks/{task_id}/tools/{name}", ticket, args,
                            attempts=2).get("content", ""))
        return cls(schemas, invoke)

    @classmethod
    def canned(cls, schemas: list[dict[str, Any]], responses: dict[str, Any]) -> "RemoteTools":
        """Offline/eval mode: replay responses keyed by tool name (a string, or a list consumed in order)."""
        queues = {k: list(v) if isinstance(v, list) else [v] for k, v in (responses or {}).items()}

        def invoke(name: str, args: dict[str, Any]) -> str:
            queue = queues.get(name)
            if not queue:
                return f"error: no recorded response for {name} in offline mode"
            return str(queue.pop(0) if len(queue) > 1 else queue[0])
        return cls(schemas, invoke)
