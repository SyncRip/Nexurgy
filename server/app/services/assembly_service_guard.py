"""Build-local admission control after independent, exhausted provider failures.

The normal tracer owns every request and retry. This guard only defers new work
after those retries have already failed; it never cancels work in flight.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


_TRANSPORT_CLASSES = frozenset({"connection", "server_error", "overloaded", "stream_broken", "no_response"})


@dataclass
class DefinitionServiceGuard:
    """Two consecutive distinct failures open one selected provider's build guard.

    Non-service outcomes break the sequence. Once opened, a later in-flight
    success is kept but cannot admit more work. A continuation creates a new guard.
    """

    provider: str
    model_id: str
    config_id: str | None
    error: dict[str, Any] | None = field(default=None, init=False)
    failed_definitions: list[str] = field(default_factory=list, init=False)
    _seen: set[str] = field(default_factory=set, init=False)

    @property
    def opened(self) -> bool:
        return self.error is not None

    def observe(self, key: str, *, status: str, error: Mapping[str, Any] | None, calls: Sequence[Any]) -> None:
        """Observe a settled definition, using its actual terminal call as proof."""
        if self.opened or key in self._seen:
            return
        self._seen.add(key)
        err = error or {}
        call = calls[-1] if calls else None
        eligible = (
            status == "failed" and err.get("kind") == "provider" and err.get("retryable") is True
            and err.get("failure_class") in _TRANSPORT_CLASSES and call is not None
            and call.status == "error" and call.failure_class == err.get("failure_class")
            and call.error_code == err.get("code") and (call.retry or {}).get("stopped") == "attempts_used"
            and call.request.get("unit") == f"definition:{key}"
            and (call.provider, call.model_id, call.model_config_id) == (self.provider, self.model_id, self.config_id)
        )
        if not eligible:
            self.failed_definitions.clear()
            return
        self.failed_definitions.append(key)
        if len(self.failed_definitions) >= 2:
            self.error = {k: err[k] for k in ("code", "message", "failure_class", "retryable") if k in err}
