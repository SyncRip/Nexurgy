"""Periodic liveness only; each pulse owns its short transaction and validates its worker identity."""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from ..ai.timeouts import HEARTBEAT_S

log = logging.getLogger("partroute.worker_heartbeat")


@contextmanager
def background_heartbeat(pulse: Callable[[], bool], *, name: str) -> Iterator[None]:
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(HEARTBEAT_S):
            try:
                if not pulse():
                    return
            except Exception:  # noqa: BLE001 - a failed beat never manufactures liveness
                log.exception("%s heartbeat failed", name)

    thread = threading.Thread(target=beat, name=name, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1.0)
