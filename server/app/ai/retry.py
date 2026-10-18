"""Bounded retry of transient model failures and the limits every model request is checked against (generation
reliability J; .trellis/tasks/archive/2026-10/10-13-generation-reliability/design.md §2.1, §2.2).

- One **logical call** (``Tracer.model_call``) makes at most :data:`MAX_ATTEMPTS` real requests. Only the transient
  failure classes (``providers.base.RETRYABLE_CLASSES``) are retried, after an exponential backoff (2 s, 4 s, … at most
  :data:`BACKOFF_MAX_S`) or the provider's ``Retry-After`` when it asks for longer. A partial reply is never carried
  into the next attempt: every attempt sends the same request again. The SDKs' own retries are off (OpenAI
  ``max_retries=0``, Gemini ``attempts=1``), so every real request is one ``ai_calls`` row with ``request.unit`` (the
  logical work unit, e.g. ``definition:D-12``, ``spec_chunk:7``, ``placement:batch:I-12``) and ``request.attempt``
  (from 1).
- :class:`CallLimits` are checked **before every attempt**: the run was cancelled, this attempt lost its node lease,
  the deadline (the node's runtime cap and the run's time budget) leaves too little time, or the run's call budget is
  used up (counted run-wide through ``CallLimits.share``, so parallel steps of one run share it). Any of them stops the
  call with a ProviderError of class ``cancelled`` / ``lease_lost`` / ``budget`` (no request is made, so no ``ai_calls``
  row) — also before a backoff wait, which a cancel cuts short (:func:`wait`). Callers that start work units
  (``orchestration.build_assembly``) check :meth:`CallLimits.stop_reason` before each unit as well.
- The limits of the running workflow node are put in place by the engine (``limits_scope``) and read by every tracer
  in that thread; a tracer may also hold its own. Default: no limits (interactive callers keep working as before).
  ``unit_scope`` names the logical unit of the calls made inside it.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from .providers.base import RETRYABLE_CLASSES, STOP_CLASSES, ProviderError, classify

#: Real requests of one logical call at most (the first one included).
MAX_ATTEMPTS = 3
#: Longest wait between two attempts from the backoff.
BACKOFF_MAX_S = 20.0
#: A provider asking to wait longer than this (Retry-After) is not waited out inside a step: the call fails and the run
#: continues later.
RETRY_AFTER_MAX_S = 60.0
#: An attempt is not started with less than this left before the deadline.
MIN_ATTEMPT_S = 5.0
#: The wait between attempts is cut into slices this long so a cancel is noticed during it.
WAIT_SLICE_S = 1.0
#: Deterministic CAD kernel failures (the same op fails the same way every time): the modeling agent returns them at
#: once — no repair round — and the work unit is ``unsupported`` (design §2.1, §2.7).
KERNEL_UNSUPPORTED_CODES = frozenset({"unsupported_font"})
#: Outcome codes of the class ``unsupported``.
UNSUPPORTED_CODES = frozenset({"unsupported"}) | KERNEL_UNSUPPORTED_CODES


def backoff_s(attempt: int) -> float:
    """Wait after the ``attempt``-th failed request: base × 2^(attempt-1), at most :data:`BACKOFF_MAX_S` (base 2 s,
    ``PARTROUTE_AI_RETRY_BACKOFF_S``)."""
    from ..config import get_settings

    base = float(get_settings().ai_retry_backoff_s)
    return min(base * 2 ** max(0, attempt - 1), BACKOFF_MAX_S)


def sleep(seconds: float) -> None:
    """The wait between attempts (a module function so tests can replace it)."""
    if seconds > 0:
        time.sleep(seconds)


def wait(seconds: float, limits: CallLimits | None) -> Stop | None:
    """The wait before another attempt: a limit already reached stops at once (no wait); a cancel during the wait cuts
    it short (checked about every :data:`WAIT_SLICE_S`). Returns the stop, or None after the full wait."""
    if limits is not None and (stop := limits.stop_reason()) is not None:
        return stop
    if limits is None or limits.cancelled is None:
        sleep(seconds)
        return None
    left = seconds
    while left > 0:
        piece = min(WAIT_SLICE_S, left)
        sleep(piece)
        left -= piece
        if left > 0 and limits.cancelled():
            return _CANCELLED
    return None


@dataclass
class CallLimits:
    """The limits model requests are checked against (design §2.2). ``deadline`` is monotonic (the earliest of the
    node's runtime cap and the run's time budget); ``max_calls`` / ``used_calls`` count real requests of the run
    (``used_calls`` starts at what the run used before this step). ``cancelled`` and ``lease_ok`` are live checks.
    Shared by the threads of one step (chunk reads), so counting is locked.

    ``share(max_calls, take)``: the run-wide request counter every step of the run uses (parallel steps on other workers
    included): with ``take`` it takes one request — ``(taken, run-wide count)`` — else it reads the count; None when it is
    unavailable (this step's own count decides then)."""

    deadline: float | None = None
    # "run" (the run's max_seconds), "node" (the step's runtime cap) or "tool" (an agent attempt's time limit)
    deadline_kind: str = "time"
    max_calls: int | None = None
    used_calls: int = 0
    cancelled: Callable[[], bool] | None = None
    lease_ok: Callable[[], bool] | None = None
    share: Callable[[int, bool], tuple[bool, int] | None] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # held while a request is counted and its unit marked, so :meth:`stop_reason` sees both or neither
    _count_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # the work units that made a request through these limits
    _requested: set[str] = field(default_factory=set, repr=False)

    def remaining_s(self) -> float | None:
        return None if self.deadline is None else self.deadline - time.monotonic()

    def stop_reason(self, *, reserve_s: float = 0.0, calls: bool = True, running: Iterable[str] = ()) -> Stop | None:
        """The first limit that stops new work, else None. ``reserve_s``: the time a new unit needs; ``running``: the work
        units already started — one request is set aside for each that has not made one yet (each makes at least one),
        so a new unit starts only when the call budget has a request left for it too."""
        if self.cancelled is not None and self.cancelled():
            return _CANCELLED
        if self.lease_ok is not None and not self.lease_ok():
            return Stop("lease_lost", "this attempt no longer holds the step (its lease went to another worker)", "lease_lost")
        left = self.remaining_s()
        if left is not None and left < max(reserve_s, MIN_ATTEMPT_S):
            run = self.deadline_kind == "run"
            what = {"run": "the run's time budget", "tool": "the agent attempt's time limit"}.get(self.deadline_kind, "the step's runtime limit")
            return Stop("budget_exhausted", f"{what} is used up ({max(0.0, left):.0f} s left)", "run_time" if run else "node_time")
        if calls and self.max_calls is not None:
            with self._count_lock:
                shared = self.share(self.max_calls, False) if self.share is not None else None
                with self._lock:
                    if shared is not None:
                        self.used_calls = max(self.used_calls, shared[1])
                    promised = sum(1 for u in running if u not in self._requested)
                    if self.used_calls + promised >= self.max_calls:
                        return self._calls_stop()
        return None

    def _calls_stop(self) -> Stop:
        return Stop("budget_exhausted", f"the run's call budget is used up ({self.used_calls} of {self.max_calls} model requests)", "calls")

    def take(self, unit: str | None = None) -> ProviderError | None:
        """Before one real request of the work unit ``unit``: the stop error when a limit is reached, else None and the
        request is counted (in the run-wide counter when there is one, so parallel steps share the budget)."""
        stop = self.stop_reason(calls=False)
        if stop is not None:
            return stop.error()
        with self._count_lock:
            if self.max_calls is not None and self.share is not None and (shared := self.share(self.max_calls, True)) is not None:
                taken, used = shared
                with self._lock:
                    self.used_calls = used
                if not taken:
                    return self._calls_stop().error()
            else:
                with self._lock:
                    if self.max_calls is not None and self.used_calls >= self.max_calls:  # another thread took the last one
                        return self._calls_stop().error()
                    self.used_calls += 1
            if unit:
                with self._lock:
                    self._requested.add(unit)
        return None

    def cap_timeout(self, timeout_s: float) -> float:
        """An attempt never runs past the deadline."""
        left = self.remaining_s()
        return timeout_s if left is None else max(1.0, min(timeout_s, left))


@dataclass(frozen=True)
class Stop:
    """Why no new work starts: ``code`` cancelled / lease_lost / budget_exhausted; ``kind`` cancelled / lease_lost /
    calls (the run's call budget) / run_time (the run's time budget) / node_time (the step's runtime cap)."""

    code: str
    message: str
    kind: str

    def error(self) -> ProviderError:
        """The error of a request that is not started (no ai_calls row): class cancelled / lease_lost / budget."""
        return ProviderError(self.code, self.message, retryable=False)  # type: ignore[arg-type]


_CANCELLED = Stop("cancelled", "the run was cancelled; no new work is started", "cancelled")

_LIMITS: ContextVar[CallLimits | None] = ContextVar("partroute_call_limits", default=None)
_UNIT: ContextVar[str | None] = ContextVar("partroute_call_unit", default=None)


@contextmanager
def limits_scope(limits: CallLimits | None) -> Iterator[CallLimits | None]:
    token = _LIMITS.set(limits)
    try:
        yield limits
    finally:
        _LIMITS.reset(token)


@contextmanager
def unit_scope(unit: str | None) -> Iterator[None]:
    token = _UNIT.set(unit)
    try:
        yield
    finally:
        _UNIT.reset(token)


def current_limits() -> CallLimits | None:
    return _LIMITS.get()


class _DeadlineLimits(CallLimits):
    """An earlier deadline over the same counter, cancellation and lease checks; never a fresh call budget."""

    def __init__(self, parent: CallLimits | None, deadline: float, deadline_kind: str,
                 check: Callable[[], None] | None) -> None:
        if parent is not None and parent.deadline is not None and parent.deadline <= deadline:
            deadline, deadline_kind = parent.deadline, parent.deadline_kind
        super().__init__(deadline=deadline, deadline_kind=deadline_kind,
                         cancelled=parent.cancelled if parent is not None else None)
        self.parent = parent
        self.check = check

    def stop_reason(self, **kwargs: Any) -> Stop | None:
        if self.check is not None:
            self.check()
        return (self.parent.stop_reason(**kwargs) if self.parent is not None else None) or super().stop_reason(**kwargs)

    def take(self, unit: str | None = None) -> ProviderError | None:
        stop = self.stop_reason(calls=False)
        if stop is not None:
            return stop.error()
        return self.parent.take(unit) if self.parent is not None else super().take(unit)


@contextmanager
def deadline_scope(deadline: float, *, deadline_kind: str = 'tool',
                   check: Callable[[], None] | None = None) -> Iterator[CallLimits]:
    """Clamp nested requests and share accounting; a live guard runs before any request is charged or sent."""
    bounded = _DeadlineLimits(current_limits(), deadline, deadline_kind, check)
    with limits_scope(bounded):
        yield bounded


def current_unit() -> str | None:
    return _UNIT.get()


def next_delay(err: ProviderError, attempt: int, limits: CallLimits | None) -> tuple[float | None, str | None]:
    """``(seconds to wait, None)`` before another attempt, or ``(None, why not)``: not a transient class, the attempts
    are used up, the provider asks to wait too long, or the deadline would pass."""
    if err.failure_class not in RETRYABLE_CLASSES or not err.retryable:
        return None, "not_transient"
    if attempt >= MAX_ATTEMPTS:
        return None, "attempts_used"
    delay = backoff_s(attempt)
    if err.retry_after_s is not None:
        if err.retry_after_s > RETRY_AFTER_MAX_S:
            return None, "retry_after_too_long"
        delay = max(delay, float(err.retry_after_s))
    left = limits.remaining_s() if limits is not None else None
    if left is not None and left - delay < MIN_ATTEMPT_S:
        return None, "deadline"
    return delay, None


# --- reading the ledger --------------------------------------------------------------------------------------------


def row_class(error: Any) -> str | None:
    """The failure class of a stored ``ai_calls.error`` (rows written before J carry only the code)."""
    if not isinstance(error, dict):
        return None
    return error.get("failure_class") or classify(error.get("code"))


def summarize(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """``calls: {units, requests, retries, failed, by_class}`` of a run (design §2.1): logical work units apart from real
    requests. A row is ``{scope, unit, attempt, status, error}``: ``scope`` keeps units of different steps apart (the
    node run), ``unit`` defaults to the call's purpose; rows written before J have no unit / attempt (each its own
    attempt 1)."""
    units: set[tuple[Any, Any]] = set()
    requests = retries = failed = 0
    by_class: Counter[str] = Counter()
    for r in rows:
        requests += 1
        units.add((r.get("scope"), r.get("unit") or r.get("purpose")))
        try:
            attempt = int(r.get("attempt") or 1)
        except (TypeError, ValueError):
            attempt = 1
        if attempt > 1:
            retries += 1
        if r.get("status") != "ok":
            failed += 1
            cls = row_class(r.get("error"))
            if cls:
                by_class[cls] += 1
    return {"units": len(units), "requests": requests, "retries": retries, "failed": failed, "by_class": dict(sorted(by_class.items()))}


def retries_spent(calls: Iterable[Any]) -> bool:
    """Whether the last failed call record of a step already went through the bounded retry for a transient class (or
    was stopped by a limit): the engine then adds no node attempt on top, so the two retries never multiply."""
    last = None
    for c in calls:
        status = getattr(c, "status", None) if not isinstance(c, dict) else c.get("status")
        if status != "ok":
            last = c
    if last is None:
        return False
    cls = getattr(last, "failure_class", None) if not isinstance(last, dict) else last.get("failure_class")
    return cls in RETRYABLE_CLASSES or cls in STOP_CLASSES


def outcome_class(code: str | None, steps: Iterable[dict[str, Any]] | None = None) -> str | None:
    """The failure class of a work unit's outcome (not a provider error): deterministic ``unsupported``, a CAD kernel
    failure (``geometry_error``: the last failing step built geometry, or the result missed its requirements), a reply
    that never validated (``unparsable``), or the class of a provider / stop code. None when unknown."""
    if not code:
        return None
    if code in UNSUPPORTED_CODES:
        return "unsupported"
    if code == "requirements_not_met":
        return "geometry_error"
    if code in ("budget_exhausted", "cancelled", "lease_lost", "auth_failed", "permission_denied", "quota_exceeded", "rate_limited",
                "network", "timeout", "invalid_request", "unsupported_capability", "model_not_found", "missing_key", "bad_response"):
        return classify(code)
    last_failed = None
    for s in steps or []:
        if isinstance(s, dict) and s.get("ok") is False:
            last_failed = s.get("kind")
    if last_failed in ("build", "check"):
        return "geometry_error"
    if last_failed in ("validation", "repair"):
        return "unparsable"
    return None
