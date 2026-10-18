"""Call ledger for business checks (AC-T03; generation reliability N, design §2.8).

:func:`app.policy.run_check` records the dotted check name each time a business checker really runs, and (Turbo) each
time it skips one without calling it.

- :func:`capture`: tests wrap a Turbo run and assert the engineering / requirement / drawing checks are absent. Process
  wide; only the checks that really ran.
- :func:`scope`: the checks of one unit of work, attributable to it — one workflow node attempt
  (``workflow/handlers.HANDLERS``) or one agent tool call (``services/agent_tools.run_tool``). Context-local (a
  ``ContextVar``), so parallel nodes in other worker threads never mix. The node keeps its scope as one step of kind
  :data:`STEP_KIND` per attempt; a delivery adds them up for its run (``services/verification.checks_report``).

Recording is a no-op unless something is capturing or scoping, so a call outside any unit pays nothing.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

_lock = threading.Lock()
_buffers: list[list[str]] = []

#: The node-run step that keeps one attempt's ledger (``node_runs.steps``); the run API lists it apart.
STEP_KIND = "checks_ledger"


@dataclass
class Ledger:
    """The checks one unit of work really ran (``invoked``) and the ones the policy closed (``skipped``), in order."""

    invoked: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, dict[str, int]]:
        return {"invoked": dict(sorted(Counter(self.invoked).items())), "skipped": dict(sorted(Counter(self.skipped).items()))}


_scopes: ContextVar[tuple[Ledger, ...]] = ContextVar("partroute_checks_ledger", default=())


def record(check: str) -> None:
    """A business checker really ran."""
    for led in _scopes.get():
        led.invoked.append(check)
    if not _buffers:
        return
    with _lock:
        for buf in _buffers:
            buf.append(check)


def record_skipped(check: str) -> None:
    """A business check the policy closed was not called (Turbo). Scopes only: :func:`capture` lists real calls."""
    for led in _scopes.get():
        led.skipped.append(check)


@contextmanager
def capture() -> Iterator[list[str]]:
    buf: list[str] = []
    with _lock:
        _buffers.append(buf)
    try:
        yield buf
    finally:
        with _lock:
            _buffers.remove(buf)


@contextmanager
def scope() -> Iterator[Ledger]:
    """The ledger of one unit of work; nested scopes all see the inner records."""
    led = Ledger()
    token = _scopes.set((*_scopes.get(), led))
    try:
        yield led
    finally:
        _scopes.reset(token)


def current() -> Ledger | None:
    """The innermost open scope (the unit of work running now), None outside any."""
    scopes = _scopes.get()
    return scopes[-1] if scopes else None


def step(led: Ledger) -> dict[str, Any]:
    """The node step that keeps one attempt's ledger (the engine adds the attempt number)."""
    return {"kind": STEP_KIND, "ok": True, "at": datetime.now(UTC).isoformat(), "detail": led.summary()}


def public_steps(steps: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """A node run's steps without its ledger steps (the run API lists those apart)."""
    return [s for s in steps or [] if not (isinstance(s, dict) and s.get("kind") == STEP_KIND)]


def from_steps(steps: Iterable[dict[str, Any]] | None) -> dict[str, dict[str, int]] | None:
    """The ledger kept on a node run (every attempt added up); None when no attempt of it recorded one."""
    found = False
    invoked: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    for s in steps or []:
        if not (isinstance(s, dict) and s.get("kind") == STEP_KIND):
            continue
        found = True
        detail = s.get("detail") if isinstance(s.get("detail"), dict) else {}
        invoked.update({str(k): int(v) for k, v in (detail.get("invoked") or {}).items()})
        skipped.update({str(k): int(v) for k, v in (detail.get("skipped") or {}).items()})
    if not found:
        return None
    return {"invoked": dict(sorted(invoked.items())), "skipped": dict(sorted(skipped.items()))}
