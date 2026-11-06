"""Coding engines: each takes an instruction and edits the working tree in place. Swappable per agent."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..llm import LLMConfig


@dataclass
class EngineRun:
    ok: bool
    log_tail: str = ""


class Engine(Protocol):
    def run(self, repo: Path, instruction: str, files: list[str], attempt: int, timeout: float) -> EngineRun: ...


def get_engine(name: str, llm: LLMConfig, workdir: Path) -> Engine:
    if name == "aider":
        from .aider import AiderEngine

        return AiderEngine(llm, workdir)
    if name == "mini-swe":
        from .mini_swe import MiniSweEngine

        return MiniSweEngine(llm, workdir)
    raise ValueError(f"unknown edit engine {name!r}")
