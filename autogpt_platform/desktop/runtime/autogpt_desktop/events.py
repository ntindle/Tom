"""The runtime's half of the shell contract (see desktop/src/runtime.js).

stdout carries one JSON object per line and nothing else, so every child
service logs to a file and the runtime's own diagnostics go to stderr.
"""

from __future__ import annotations

import json
import logging
import sys
import threading

_lock = threading.Lock()

logger = logging.getLogger("autogpt_desktop")


def progress(step: str, message: str) -> None:
    logger.info(message)
    _emit({"event": "progress", "step": step, "message": message})


def ready(url: str) -> None:
    logger.info(f"AutoGPT is ready at {url}")
    _emit({"event": "ready", "url": url})


def error(message: str, *, fatal: bool) -> None:
    logger.error(message)
    _emit({"event": "error", "message": message, "fatal": fatal})


def _emit(payload: dict[str, object]) -> None:
    line = json.dumps(payload, separators=(",", ":"))
    with _lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def configure_logging() -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s [desktop] %(levelname)s %(message)s",
    )
