"""Workflow workers.

Inline: the API process starts a WorkerPool (PARTROUTE_INLINE_WORKER=true).
Standalone: `python -m app.worker` runs the same loop as its own service.
The first thread also dispatches outbox events into notifications.
Agent attempts (collaboration queue) run only on their own threads (PARTROUTE_AGENT_THREADS, default 1): a
multi-step agent never holds a workflow thread, and workflow threads never claim agent attempts.
AI native: main-agent turns of the AI workspace run on their own threads too (PARTROUTE_ASSISTANT_THREADS, default 1);
the first one also follows active sub-agents, so a finished one wakes its main agent with no page open.
"""

from __future__ import annotations

import logging
import signal
import threading
import time

from .config import get_settings
from .db import session_scope
from .workflow import engine

log = logging.getLogger("partroute.worker")

OUTBOX_EVERY_SECONDS = 2.0
#: How often the first assistant thread follows the runs of active sub-agents (design §2.6).
FOLLOW_EVERY_SECONDS = 2.0


def dispatch_outbox(limit: int = 100) -> int:
    """One outbox pass in its own transaction; never raises into the loop."""
    from .services.outbox import dispatch_pending

    try:
        with session_scope() as db:
            return dispatch_pending(db, limit)
    except Exception:  # noqa: BLE001 - notifications must not stop the workers
        log.exception("outbox dispatch failed")
        return 0


def run_one_agent() -> bool:
    """One agent execution / review from the collaboration queue; False when idle.
    The attempt records its own failures; this never raises into the loop."""
    from .services import agent_runtime

    try:
        return agent_runtime.claim_and_run_one()
    except Exception:  # noqa: BLE001
        log.exception("agent runtime step failed")
        return False


def recover_agent_leases() -> int:
    from .services import agent_runtime

    try:
        with session_scope() as db:
            n = agent_runtime.recover_expired(db)
        if n:
            log.warning("recovered %d expired agent executions", n)
        return n
    except Exception:  # noqa: BLE001
        log.exception("agent lease recovery failed")
        return 0


def _handlers():
    from .workflow.handlers import HANDLERS

    return HANDLERS


class WorkerPool:
    def __init__(self, threads: int = 2, poll_seconds: float | None = None, agent_threads: int | None = None,
                 assistant_threads: int | None = None):
        self.threads = max(1, threads)
        self.agent_threads = max(0, get_settings().agent_threads if agent_threads is None else agent_threads)
        self.assistant_threads = max(0, get_settings().assistant_threads if assistant_threads is None else assistant_threads)
        self.poll = poll_seconds or get_settings().worker_poll_seconds
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        from .services.feature_flags import record_rollout

        get_settings().validate_runtime_environment()
        with session_scope() as db:
            record_rollout(db, source="worker")
        handlers = _handlers()
        for i in range(self.threads):
            t = threading.Thread(target=self._loop, args=(handlers, i), name=f"partroute-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        for i in range(self.agent_threads):
            t = threading.Thread(target=self._agent_loop, name=f"partroute-agent-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        for i in range(self.assistant_threads):
            t = threading.Thread(target=self._assistant_loop, name=f"partroute-assistant-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        if self.assistant_threads:
            # Its own thread: a turn that runs for minutes never delays the wake-up of another conversation's main agent.
            t = threading.Thread(target=self._follow_loop, name="partroute-assistant-follow", daemon=True)
            t.start()
            self._threads.append(t)
        log.info("worker pool started with %d workflow, %d agent and %d assistant threads", self.threads, self.agent_threads,
                 self.assistant_threads)

    def stop(self, timeout: float = 10) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=timeout)

    def _loop(self, handlers, index: int) -> None:
        last_recover = 0.0
        last_outbox = 0.0
        while not self._stop.is_set():
            try:
                if index == 0 and time.monotonic() - last_recover > 15:
                    with session_scope() as db:
                        n = engine.recover_expired(db)
                    if n:
                        log.warning("recovered %d expired node runs", n)
                    recover_agent_leases()
                    last_recover = time.monotonic()
                if index == 0 and time.monotonic() - last_outbox > OUTBOX_EVERY_SECONDS:
                    dispatch_outbox()
                    last_outbox = time.monotonic()
                with session_scope() as db:
                    nr = engine.claim_next(db)
                    nid = nr.id if nr else None
                if nid is None:
                    self._stop.wait(self.poll)
                    continue
                engine.execute_claimed(nid, handlers)
            except Exception:  # keep the loop alive; the node itself records failures
                log.exception("worker loop error")
                self._stop.wait(self.poll)

    def _agent_loop(self) -> None:
        """Agent attempts only (design §6.6): claim one, run it to its write-back, repeat; idle → wait."""
        while not self._stop.is_set():
            if not run_one_agent():
                self._stop.wait(self.poll)

    def _assistant_loop(self) -> None:
        """AI native (design §2.6): main-agent turns only — claim one, run it to its end, repeat; idle → wait (never raises)."""
        from .services import orchestrator

        while not self._stop.is_set():
            if not orchestrator.run_next_turn():
                self._stop.wait(self.poll)

    def _follow_loop(self) -> None:
        """AI native: follow the active sub-agents every FOLLOW_EVERY_SECONDS, so a finished one wakes its main agent with no
        page open (never raises)."""
        from .services import orchestrator

        while not self._stop.wait(FOLLOW_EVERY_SECONDS):
            orchestrator.follow_children()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    s = get_settings()
    pool = WorkerPool(threads=s.worker_threads, agent_threads=s.agent_threads, assistant_threads=s.assistant_threads)
    pool.start()
    done = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: done.set())
    signal.signal(signal.SIGINT, lambda *_: done.set())
    done.wait()
    pool.stop()


if __name__ == "__main__":
    main()
