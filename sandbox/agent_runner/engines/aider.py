"""Aider in headless one-shot mode, pointed at vLLM, with every network side-channel disabled."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from ..llm import LLMConfig
from . import EngineRun

AGENT_HOME = Path(os.environ.get("AGENT_HOME", "/opt/agent"))
# Aider prints one of these per LLM call, e.g. "Tokens: 12k sent, 1.2k cache hit, 345 received. Cost: ..."
_TOKENS = re.compile(r"^Tokens: ([\d.,]+[kKmM]?) sent,(?:.*?,)? ([\d.,]+[kKmM]?) received", re.MULTILINE)


def _count(text: str) -> int:
    text = text.replace(",", "")
    scale = {"k": 1_000, "m": 1_000_000}.get(text[-1].lower(), 1)
    return int(float(text.rstrip("kKmM")) * scale)


def parse_usage(output: str) -> dict[str, int]:
    """Token usage from Aider's output (its 'Tokens: ... sent, ... received' line after every LLM call)."""
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
    for sent, received in _TOKENS.findall(output):
        usage["prompt_tokens"] += _count(sent)
        usage["completion_tokens"] += _count(received)
        usage["calls"] += 1
    return usage


class AiderEngine:
    def __init__(self, llm: LLMConfig, workdir: Path):
        self.llm = llm
        self.workdir = workdir  # scratch space outside the repo for histories and prompt files
        self.binary = os.environ.get("AGENT_AIDER_BIN") or shutil.which("aider") or str(AGENT_HOME / "venv/bin/aider")

    def command(self, message_file: Path, files: list[str], attempt: int) -> list[str]:
        cfg_dir = AGENT_HOME / "aider"
        cmd = [
            self.binary,
            "--model", f"openai/{self.llm.model}",
            "--message-file", str(message_file),
            "--yes-always",
            "--no-auto-commits", "--no-dirty-commits",
            "--no-check-update", "--analytics-disable", "--no-show-release-notes",
            "--no-show-model-warnings", "--no-pretty", "--no-fancy-input", "--no-stream",
            "--no-gitignore", "--no-suggest-shell-commands", "--no-detect-urls", "--no-auto-lint",
            "--map-tokens", os.environ.get("AGENT_AIDER_MAP_TOKENS", "2048"),
            "--input-history-file", str(self.workdir / "aider.input.history"),
            "--chat-history-file", str(self.workdir / "aider.chat.history.md"),
            "--llm-history-file", str(self.workdir / "aider.llm.history"),
            "--timeout", "600",
        ]
        if (cfg_dir / "model-settings.yml").is_file():
            cmd += ["--model-settings-file", str(cfg_dir / "model-settings.yml")]
        if (cfg_dir / "model-metadata.json").is_file():
            cmd += ["--model-metadata-file", str(cfg_dir / "model-metadata.json")]
        edit_format = os.environ.get("AGENT_AIDER_EDIT_FORMAT")
        if edit_format:
            cmd += ["--edit-format", edit_format]
        if attempt > 1:
            cmd.append("--restore-chat-history")  # keep the earlier attempts' reasoning in context
        return cmd + files

    def run(self, repo: Path, instruction: str, files: list[str], attempt: int, timeout: float) -> EngineRun:
        message_file = self.workdir / f"aider-message-{attempt}.md"
        message_file.write_text(instruction, encoding="utf-8")
        env = {
            **os.environ,
            "OPENAI_API_BASE": self.llm.base_url,
            "OPENAI_API_KEY": self.llm.api_key,
            "AIDER_ANALYTICS": "false",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "TIKTOKEN_CACHE_DIR": os.environ.get("TIKTOKEN_CACHE_DIR", str(AGENT_HOME / "tiktoken")),
            "HOME": str(self.workdir / "home"),
        }
        if self.llm.ca_file:
            env["SSL_CERT_FILE"] = self.llm.ca_file
        (self.workdir / "home").mkdir(exist_ok=True)
        try:
            proc = subprocess.run(
                self.command(message_file, files, attempt), cwd=repo, env=env, capture_output=True, text=True,
                errors="replace", timeout=max(timeout, 60), stdin=subprocess.DEVNULL,
            )
            out = proc.stdout + proc.stderr
            return EngineRun(ok=proc.returncode == 0, log_tail=out[-6000:], usage=parse_usage(proc.stdout))
        except subprocess.TimeoutExpired:
            return EngineRun(ok=False, log_tail=f"aider timed out after {timeout:.0f}s")
