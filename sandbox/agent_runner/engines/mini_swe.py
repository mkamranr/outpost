"""mini-SWE-agent: a small bash-only agent loop. Alternative engine; select with `engine: mini-swe`."""
from __future__ import annotations

import os
from pathlib import Path

from ..llm import LLMConfig
from . import EngineRun


def message_usage(agent: object) -> dict[str, int] | None:
    """Best effort: recent mini-swe-agent releases keep each raw litellm response in the message's `extra`."""
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
    for message in getattr(agent, "messages", None) or []:
        response = ((message.get("extra") or {}).get("response") or {}) if isinstance(message, dict) else {}
        if isinstance(response, dict) and isinstance(response.get("usage"), dict):
            usage["calls"] += 1
            usage["prompt_tokens"] += int(response["usage"].get("prompt_tokens") or 0)
            usage["completion_tokens"] += int(response["usage"].get("completion_tokens") or 0)
    if not usage["calls"]:
        calls = getattr(getattr(agent, "model", None), "n_calls", 0)
        return {"prompt_tokens": 0, "completion_tokens": 0, "calls": int(calls)} if calls else None
    return usage


class MiniSweEngine:
    def __init__(self, llm: LLMConfig, workdir: Path):
        self.llm = llm
        self.workdir = workdir

    def run(self, repo: Path, instruction: str, files: list[str], attempt: int, timeout: float) -> EngineRun:
        os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")
        try:
            from minisweagent.agents.default import DefaultAgent
            from minisweagent.environments.local import LocalEnvironment
            from minisweagent.models.litellm_model import LitellmModel
        except ImportError as exc:
            return EngineRun(ok=False, log_tail=f"mini-swe-agent is not installed in this image: {exc}")

        model_kwargs = {"api_base": self.llm.base_url, "api_key": self.llm.api_key, "temperature": self.llm.temperature}
        # Config field names have moved between mini-swe-agent releases; fall back to the minimal signature.
        try:
            model = LitellmModel(model_name=f"openai/{self.llm.model}", model_kwargs=model_kwargs,
                                 cost_tracking="ignore_errors")
        except TypeError:
            model = LitellmModel(model_name=f"openai/{self.llm.model}", model_kwargs=model_kwargs)
        try:
            env = LocalEnvironment(cwd=str(repo), timeout=int(min(timeout, 600)))
        except TypeError:
            os.chdir(repo)
            env = LocalEnvironment()
        step_limit = int(os.environ.get("AGENT_MINI_SWE_STEP_LIMIT", "40"))
        try:
            agent = DefaultAgent(model, env, step_limit=step_limit, cost_limit=0)
        except TypeError:
            agent = DefaultAgent(model, env)
        task = instruction
        if files:
            task += "\n\nFiles most likely involved: " + ", ".join(files)
        task += "\n\nWork in the current directory. Do not commit. Finish when the change is complete."
        try:
            status, message = agent.run(task)
            return EngineRun(ok=status in ("Submitted", "submitted"), log_tail=f"{status}: {str(message)[-4000:]}",
                             usage=message_usage(agent))
        except Exception as exc:  # noqa: BLE001 - the step/time limits surface as exceptions
            return EngineRun(ok=False, log_tail=f"mini-swe-agent stopped: {type(exc).__name__}: {exc}"[:4000],
                             usage=message_usage(agent))
