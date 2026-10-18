"""Bounded defaults for long CAD requests; enclosing and explicit limits still win."""
from __future__ import annotations

CAD_MODEL_CALL_S = 15 * 60
MAIN_TURN_S = 60 * 60
SPEC_EXTRACTION_S = 55 * 60
MAIN_QUEUE_STALE_S = 2 * MAIN_TURN_S
SHORT_STREAM_IDLE_S = 60.0
HEARTBEAT_S = 30.0


def stream_idle_seconds(timeout_s: float, override_s: float | None = None, *,
                        short_idle_s: float = SHORT_STREAM_IDLE_S) -> float:
    """Long reasoning can pause after metadata; a shorter request/idle override remains authoritative."""
    idle = override_s if override_s is not None else (CAD_MODEL_CALL_S if timeout_s > 10 * 60 else short_idle_s)
    return min(timeout_s, idle)
