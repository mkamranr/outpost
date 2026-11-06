#!/usr/bin/env python3
"""Deterministic OpenAI-compatible stub used by build/airgap-check.sh and local integration tests.

It answers just well enough to drive every agent-runner code path without a GPU:
  * requests with tools        -> a `submit` tool call (note, review or investigation-brief shape)
  * classification prompts     -> {"classification": "code", ...}
  * MR title/summary requests  -> {"title": ..., "summary": ...}
  * anything else (Aider)      -> a SEARCH/REPLACE edit fixing the eval off-by-one fixture
"""
from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AIDER_EDIT = """The bug is the stray `+ 1` in `add`.

calc.py
```python
<<<<<<< SEARCH
    return a + b + 1
=======
    return a + b
>>>>>>> REPLACE
```
"""


def answer(payload: dict) -> dict:
    messages = payload.get("messages") or []
    text = "\n".join(str(m.get("content") or "") for m in messages)
    if payload.get("tools"):
        names = {t["function"]["name"] for t in payload["tools"]}
        schema = next(t["function"]["parameters"] for t in payload["tools"] if t["function"]["name"] == "submit")
        props = schema.get("properties", {})
        if "comments" in props:
            args = {"summary": "Stub review.", "comments": []}
        elif "findings" in props:  # an edit agent's investigation before it edits
            args = {"findings": "Stub: add() has an off-by-one.", "suspect_files": ["calc.py"]}
        else:
            args = {"body": "## Plan\n1. Stub plan generated offline."}
        assert "submit" in names
        return {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "submit", "arguments": json.dumps(args)}}]}
    if "classify" in text.lower() and "SEARCH/REPLACE" not in text:
        return {"role": "assistant", "content": json.dumps(
            {"classification": "code", "reason": "stub: arithmetic bug", "suspect_files": ["calc.py"]})}
    if '"title"' in text and '"summary"' in text:
        return {"role": "assistant", "content": json.dumps({"title": "Fix off-by-one in add()", "summary": "Stub summary."})}
    return {"role": "assistant", "content": AIDER_EDIT}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            self._send(200, {"object": "list", "data": [{"id": "qwen3.8-27b", "object": "model"}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send(404, {"error": "not found"})
            return
        if payload.get("stream"):
            self._send(400, {"error": "stub does not stream; run clients with streaming disabled"})
            return
        message = answer(payload)
        self._send(200, {
            "id": "stub", "object": "chat.completion", "created": int(time.time()), "model": payload.get("model"),
            "choices": [{"index": 0, "message": message,
                         "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    def log_message(self, fmt: str, *args) -> None:
        print("stub-llm:", fmt % args, flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    port = ap.parse_args().port
    print(f"stub LLM listening on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
