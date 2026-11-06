"""Background worker: dispatch queued tasks, apply sandbox results, enforce timeouts.

Run with `python -m app.worker`. Several workers may run against Postgres; claims use SKIP LOCKED.
"""
from __future__ import annotations

import logging
import signal
import time

from .main import build_platform

log = logging.getLogger("orchestrator.worker")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    platform = build_platform()
    stop = False

    def _stop(*_: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log.info("worker started: %d agents, max %d concurrent sandboxes",
             len(platform.registry.agents), platform.settings.max_concurrent_sandboxes)
    while not stop:
        try:
            platform.run_once()
        except Exception:  # noqa: BLE001 - keep the loop alive; individual tasks record their own errors
            log.exception("worker cycle failed")
        time.sleep(platform.settings.poll_interval_seconds)


if __name__ == "__main__":
    main()
