"""Minimal OpenAI-compatible chat client for vLLM (stdlib only, no network beyond the configured endpoint)."""
from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


@dataclass
class LLMConfig:
    base_url: str
    model: str
    api_key: str = "EMPTY"
    temperature: float = 0.2
    max_tokens: int = 4096
    timeout: float = 600.0
    ca_file: str | None = None

    @classmethod
    def from_env(cls) -> "LLMConfig":
        base = os.environ.get("AGENT_LLM_BASE_URL")
        model = os.environ.get("AGENT_LLM_MODEL")
        if not base or not model:
            raise RuntimeError("AGENT_LLM_BASE_URL and AGENT_LLM_MODEL must be set")
        return cls(
            base_url=base.rstrip("/"),
            model=model,
            api_key=os.environ.get("AGENT_LLM_API_KEY", "EMPTY"),
            temperature=float(os.environ.get("AGENT_LLM_TEMPERATURE", "0.2")),
            max_tokens=int(os.environ.get("AGENT_LLM_MAX_TOKENS", "4096")),
            ca_file=os.environ.get("AGENT_LLM_CA_FILE") or None,
        )


@dataclass
class Reply:
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    raw_message: dict[str, Any] = field(default_factory=dict)


def strip_thinking(text: str | None) -> str:
    return _THINK.sub("", text or "").strip()


def extract_json(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model reply (tolerates code fences and surrounding prose)."""
    text = strip_thinking(text)
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1) + "\n" + text
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            value, _ = decoder.raw_decode(text, start)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
        start = text.find("{", start + 1)
    return None


class LLM:
    def __init__(self, config: LLMConfig, transport: Any = None):
        self.config = config
        self._transport = transport  # tests inject a callable(payload) -> response dict
        self._ctx = ssl.create_default_context(cafile=config.ca_file) if config.ca_file else None

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._transport:
            return self._transport(payload)
        req = urllib.request.Request(
            f"{self.config.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.config.api_key}"},
            method="POST",
        )
        last: Exception | None = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=self.config.timeout, context=self._ctx) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                if exc.code < 500 and exc.code != 429:
                    raise RuntimeError(f"LLM request failed: {exc.code} {exc.read()[:500]!r}") from exc
                last = exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last = exc
            time.sleep(5 * (attempt + 1))
        raise RuntimeError(f"LLM endpoint unavailable: {last}")

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
             max_tokens: int | None = None) -> Reply:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": max_tokens or self.config.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        data = self._post(payload)
        message = data["choices"][0]["message"]
        return Reply(
            content=strip_thinking(message.get("content")),
            tool_calls=message.get("tool_calls") or [],
            raw_message=message,
        )
