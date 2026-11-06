#!/usr/bin/env python3
"""Smoke-test the vLLM endpoint for what the agents need: chat, JSON output, and multi-step tool calling.

vLLM must be started with tool calling enabled, for Qwen models e.g.:
    vllm serve <model> --served-model-name qwen3.8-27b --enable-auto-tool-choice --tool-call-parser hermes \
        --max-model-len 65536 [--reasoning-parser qwen3] [--api-key ...]
(use the tool-call parser your Qwen release documents: `hermes` for Qwen2.5/Qwen3, `qwen3_coder` for Qwen3-Coder).

    python3 scripts/check-vllm.py --base-url http://vllm.internal:8000/v1 --model qwen3.8-27b [--api-key-file f]

stdlib only; exit code 0 means every check passed.
"""
from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import time
import urllib.request

FILES = {
    "README.md": "Calculator service. Entry point is src/app.py.",
    "src/app.py": "from src.calc import add\n\nprint(add(2, 2))\n",
    "src/calc.py": "def add(a, b):\n    return a + b + 1  # BUG\n",
}
TOOLS = [
    {"type": "function", "function": {"name": "list_files", "description": "List repository files.",
                                      "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read one file.",
                                      "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                                                     "required": ["path"]}}},
    {"type": "function", "function": {"name": "submit", "description": "Submit the final answer.",
                                      "parameters": {"type": "object",
                                                     "properties": {"buggy_file": {"type": "string"},
                                                                    "fix": {"type": "string"}},
                                                     "required": ["buggy_file", "fix"]}}},
]


def post(args, payload):
    req = urllib.request.Request(args.base_url.rstrip("/") + "/chat/completions", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {args.api_key}"})
    ctx = ssl.create_default_context(cafile=args.ca_file) if args.ca_file else None
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=300, context=ctx) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"], time.time() - t0


def strip_think(text):
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL).strip()


def check_models(args):
    req = urllib.request.Request(args.base_url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {args.api_key}"})
    ctx = ssl.create_default_context(cafile=args.ca_file) if args.ca_file else None
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        ids = [m["id"] for m in json.loads(resp.read())["data"]]
    assert args.model in ids, f"model {args.model!r} not served; available: {ids}"
    return f"served models: {ids}"


def check_chat(args):
    msg, dt = post(args, {"model": args.model, "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
                          "max_tokens": 200, "temperature": 0})
    assert "pong" in strip_think(msg.get("content")).lower(), f"unexpected reply: {msg.get('content')!r}"
    return f"{dt:.1f}s"


def check_json(args):
    msg, dt = post(args, {"model": args.model, "max_tokens": 400, "temperature": 0, "messages": [{"role": "user", "content":
                   'Classify "connection reset by peer while pulling image" as code, ci_config or flaky. '
                   'Reply with only JSON: {"classification": "..."}'}]})
    text = strip_think(msg.get("content"))
    value = json.loads(text[text.find("{"): text.rfind("}") + 1])
    assert value.get("classification") == "flaky", f"got {value}"
    return f"{dt:.1f}s"


def check_tool_loop(args):
    messages = [{"role": "system", "content": "You inspect repositories with tools, then call submit."},
                {"role": "user", "content": "Find the bug in this repository and submit the buggy file path and the fix."}]
    calls = []
    for _ in range(8):
        msg, _ = post(args, {"model": args.model, "messages": messages, "tools": TOOLS, "tool_choice": "auto",
                             "max_tokens": 1500, "temperature": 0})
        tool_calls = msg.get("tool_calls") or []
        assert tool_calls, f"model answered without a tool call: {strip_think(msg.get('content'))[:300]!r}"
        messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": tool_calls})
        for call in tool_calls:
            name = call["function"]["name"]
            fn_args = json.loads(call["function"]["arguments"] or "{}")
            calls.append(name)
            if name == "submit":
                assert "calc.py" in fn_args.get("buggy_file", ""), f"wrong file: {fn_args}"
                return f"tool sequence: {' -> '.join(calls)}"
            result = "\n".join(FILES) if name == "list_files" else FILES.get(fn_args.get("path", ""), "error: no such file")
            messages.append({"role": "tool", "tool_call_id": call.get("id", name), "content": result})
    raise AssertionError(f"no submit after 8 turns: {calls}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--api-key-file")
    ap.add_argument("--ca-file")
    args = ap.parse_args()
    args.api_key = open(args.api_key_file).read().strip() if args.api_key_file else "EMPTY"
    failures = 0
    for name, fn in [("models endpoint", check_models), ("basic chat", check_chat), ("JSON answer", check_json),
                     ("multi-step tool calling", check_tool_loop)]:
        try:
            print(f"PASS  {name}: {fn(args)}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL  {name}: {exc}")
    if failures:
        print("\nTool-calling failures usually mean vLLM lacks --enable-auto-tool-choice or uses the wrong --tool-call-parser.")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
