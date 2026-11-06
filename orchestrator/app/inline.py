"""Run inline agents (chat) inside the orchestrator.

There is no sandbox and no checkout: the model only gets the read-only GitLab tools, which the orchestrator
executes for it (with the same allowlist and budget as sandbox tool calls). Nothing is written to GitLab except
the reply, which goes through the normal output handler.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from agent_runner.explore import explore
from agent_runner.llm import LLM, LLMConfig
from agent_runner.remote import RemoteTools

from .config import Settings

SYSTEM = (
    "You are a helpful engineering assistant embedded in a self-hosted GitLab instance. You can read the project "
    "through tools (files at a ref, code search, issues, merge requests, pipelines, job logs) but you cannot change "
    "anything or run code. Look things up before answering and say so when you could not find something. "
    "When you are done, call the `submit` tool exactly once."
)

LLMFactory = Callable[[], LLM]


def llm_factory(settings: Settings) -> LLMFactory:
    def make() -> LLM:
        if not settings.llm_base_url or not settings.llm_model:
            raise RuntimeError("inline agents need LLM_BASE_URL and LLM_MODEL configured on the orchestrator")
        return LLM(LLMConfig(base_url=settings.llm_base_url.rstrip("/"), model=settings.llm_model,
                             api_key=settings.llm_api_key, ca_file=settings.llm_ca_file, timeout=180.0))
    return make


def run(llm: LLM, bundle: dict[str, Any], invoke: Callable[[str, dict[str, Any]], str]) -> dict[str, Any]:
    """Run the tool loop for one inline task and return a result shaped like a sandbox result."""
    agent = bundle["agent"]
    deadline = time.time() + agent["timeout_minutes"] * 60
    remote = RemoteTools(bundle.get("tools") or [], invoke)
    answer = explore(llm, None, bundle["prompts"]["main"], agent["output"], deadline, max_turns=20, remote=remote,
                     system=SYSTEM)
    result: dict[str, Any] = {"status": "success", "body": answer["body"], "summary": "", "attempts": [], "changes": []}
    if isinstance(answer.get("handoff"), dict):
        result["handoff"] = answer["handoff"]
    return result
