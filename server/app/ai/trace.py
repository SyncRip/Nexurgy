"""Step and model-call records shared by the agents.

``AgentStep`` rows go into ``node_runs.steps`` (design.md §8); ``AiCallRecord``
rows map 1:1 onto the ``ai_calls`` table (§4). Both are JSON-safe via
``model_dump(mode="json")`` and never contain keys or attachment bytes.

Generation reliability J (.trellis/tasks/archive/2026-10/10-13-generation-reliability/design.md §2.1): :meth:`Tracer.model_call` is one
logical call that makes up to ``retry.MAX_ATTEMPTS`` real requests — transient failure classes only, with backoff /
Retry-After, every attempt checked against the call limits first (ai/retry.py). Each request is its own record with
``request.unit`` and ``request.attempt``; a failed attempt's partial output is never part of the next request.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from . import retry
from .team_prompt import check_prompt_budget, compose_role_prompt, current_team_snapshot, role_for_schema
from .providers.base import ImagePart, Message, ModelProvider, ProviderError, StructuredResult, clip

StepKind = Literal["prompt", "model_call", "validation", "build", "check", "repair"]

MAX_MESSAGE_CHARS = 20_000
MAX_RESPONSE_CHARS = 60_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class AgentStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: StepKind
    ok: bool
    detail: dict[str, Any] = Field(default_factory=dict)
    started_at: str
    duration_ms: int = 0


class AiCallRecord(BaseModel):
    """One provider call, shaped for the ``ai_calls`` table.

    ``error`` is a short "code: message" string; ``error_code`` repeats the
    code for filtering. ``request`` holds a prompt summary (system prompt by
    version + hash, user/assistant turns clipped, attachment metadata) and, since J, the logical work unit
    (``unit``) and the attempt number (``attempt``, from 1). ``failure_class`` / ``retry`` (J): the class of a failed
    request and what came next (``{"in_s": wait before the next attempt}`` or ``{"stopped": why it was not retried}``).
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model_config_id: str | None
    provider: str
    model_id: str
    purpose: str
    request: dict[str, Any]
    response: dict[str, Any] | None = None
    status: Literal["ok", "error"]
    error: str | None = None
    error_code: str | None = None
    latency_ms: int | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    simulated: bool = False
    created_at: str
    # Provider request id of a FAILED call when the provider reported one (successful calls keep it in
    # ``response["request_id"]``); stored with the error so the run ledger can show it.
    request_id: str | None = None
    failure_class: str | None = None
    retry: dict[str, Any] | None = None


class _StepHandle:
    def __init__(self) -> None:
        self.ok = True
        self.detail: dict[str, Any] = {}


class _Retry(Exception):
    """Internal: the attempt failed transiently; another one follows after ``delay`` seconds."""

    def __init__(self, delay: float) -> None:
        super().__init__(delay)
        self.delay = delay


class Tracer:
    """Collects steps and call records for one agent run.

    ``limits`` / ``unit``: the call limits and the logical work unit of every call of this tracer. When not given, the
    ones of the current scope (``retry.limits_scope``, set by the workflow engine; ``retry.unit_scope``, set by a
    caller that runs work units), else no limits and the call's purpose as its unit."""

    def __init__(self, *, limits: retry.CallLimits | None = None, unit: str | None = None) -> None:
        self.steps: list[AgentStep] = []
        self.calls: list[AiCallRecord] = []
        self.limits = limits
        self.unit = unit

    @contextmanager
    def step(self, kind: StepKind) -> Iterator[_StepHandle]:
        handle = _StepHandle()
        started_at, t0 = _now(), time.monotonic()
        try:
            yield handle
        except BaseException:
            handle.ok = False
            raise
        finally:
            self.steps.append(
                AgentStep(kind=kind, ok=handle.ok, detail=handle.detail, started_at=started_at,
                          duration_ms=int((time.monotonic() - t0) * 1000))
            )

    def model_call(
        self,
        provider: ModelProvider,
        *,
        purpose: str,
        system: str,
        system_version: str,
        messages: list[Message],
        json_schema: dict[str, Any],
        schema_name: str,
        images: list[ImagePart] | None = None,
        temperature: float | None = None,
        timeout_s: float = 60,
        unit: str | None = None,
    ) -> StructuredResult:
        """One logical call: up to ``retry.MAX_ATTEMPTS`` requests, each recorded as a ``model_call`` step and an ai_calls
        row (``request.unit``, ``request.attempt``). Only transient failure classes are retried. Before every attempt
        the limits are checked; a stop raises ProviderError ``cancelled`` / ``lease_lost`` / ``budget_exhausted``
        without a request (a step records it, no ai_calls row). ProviderError propagates after the records are
        written, with ``attempts`` set."""
        system, team = compose_role_prompt(system, snapshot=current_team_snapshot(), role=role_for_schema(schema_name))
        budget = None
        if team:
            budget = check_prompt_budget(getattr(provider, "config", None), system, "\n".join(m.content for m in messages),
                                         json_schema, images=len(images or []))
        limits = self.limits or retry.current_limits()
        unit = unit or self.unit or retry.current_unit() or purpose
        base_request = {
            "schema_name": schema_name,
            "schema_sha256": _sha(json.dumps(json_schema, sort_keys=True)),
            "system_prompt": {"version": system_version, "sha256": _sha(system), "chars": len(system)},
            "messages": [{"role": m.role, "content": clip(m.content, MAX_MESSAGE_CHARS)} for m in messages],
            "attachments": [i.summary() for i in images or []],
            "temperature": temperature,
        }
        if team:
            base_request["engineering_team"] = team
            base_request["context_budget"] = budget
        attempt = 0
        cut: ProviderError | None = None  # a limit reached before / during the wait for the next attempt
        while True:
            attempt += 1
            stop = cut or (limits.take(unit) if limits is not None else None)
            if stop is not None:
                with self.step("model_call") as st:
                    st.ok = False
                    st.detail = {"purpose": purpose, "unit": unit, "attempt": attempt, "stopped": True,
                                 "error": {"code": stop.code, "message": clip(stop.message, 300), "failure_class": stop.failure_class}}
                stop.attempts = attempt - 1
                raise stop
            call_timeout = limits.cap_timeout(timeout_s) if limits is not None else timeout_s
            request = {**base_request, "timeout_s": call_timeout, "unit": unit, "attempt": attempt}
            try:
                return self._attempt(provider, purpose=purpose, system=system, messages=messages, json_schema=json_schema,
                                     schema_name=schema_name, images=images, temperature=temperature, timeout_s=call_timeout,
                                     request=request, limits=limits)
            except _Retry as r:
                halt = retry.wait(r.delay, limits)
                cut = halt.error() if halt is not None else None

    def _attempt(self, provider: ModelProvider, *, purpose: str, system: str, messages: list[Message], json_schema: dict[str, Any],
                 schema_name: str, images: list[ImagePart] | None, temperature: float | None, timeout_s: float,
                 request: dict[str, Any], limits: retry.CallLimits | None) -> StructuredResult:
        """One real request — always the same messages: nothing of a failed attempt is carried into the next one."""
        created_at = _now()
        # The row's own created_at is the time its transaction began (several calls of a stage share it); progress
        # timelines order calls by when they really started.
        request = {**request, "started_at": created_at}
        t0 = time.monotonic()
        attempt = int(request["attempt"])
        with self.step("model_call") as st:
            st.detail = {"purpose": purpose, "provider": provider.provider_name, "model_id": provider.model_id,
                         "turns": len(messages), "attachments": len(images or []), "unit": request["unit"], "attempt": attempt}
            try:
                res = provider.generate_structured(
                    system, messages, json_schema, schema_name=schema_name, images=images,
                    temperature=temperature, timeout_s=timeout_s,
                )
            except ProviderError as e:
                st.ok = False
                delay, why = retry.next_delay(e, attempt, limits)
                after: dict[str, Any] = {"in_s": delay} if delay is not None else {"stopped": why}
                st.detail["error"] = {"code": e.code, "message": clip(e.message, 300), "retryable": e.retryable,
                                      "failure_class": e.failure_class, "retry": after}
                self.calls.append(AiCallRecord(
                    model_config_id=provider.config_id, provider=provider.provider_name,
                    model_id=provider.model_id, purpose=purpose, request=request, response=None,
                    status="error", error=f"{e.code}: {clip(e.message, 500)}", error_code=e.code,
                    simulated=provider.simulated, created_at=created_at, request_id=getattr(e, "request_id", None),
                    latency_ms=int((time.monotonic() - t0) * 1000),  # also for failures: a timeout shows how long it ran
                    tokens_in=e.tokens_in, tokens_out=e.tokens_out,
                    failure_class=e.failure_class, retry=after,
                ))
                if delay is not None:
                    raise _Retry(delay) from None
                e.attempts = attempt
                raise
            st.ok = res.data is not None
            st.detail.update({
                "latency_ms": res.latency_ms, "tokens_in": res.tokens_in, "tokens_out": res.tokens_out,
                "structured_mode": res.structured_mode, "simulated": res.simulated,
            })
            if res.parse_error:
                st.detail["parse_error"] = clip(res.parse_error, 200)
            if res.notes:
                st.detail["notes"] = [clip(n, 200) for n in res.notes]
            self.calls.append(AiCallRecord(
                model_config_id=provider.config_id, provider=provider.provider_name,
                model_id=res.model_id, purpose=purpose, request=request,
                response=response_summary(res), status="ok", latency_ms=res.latency_ms,
                tokens_in=res.tokens_in, tokens_out=res.tokens_out, simulated=res.simulated,
                created_at=created_at,
            ))
            return res


def response_summary(res: StructuredResult) -> dict[str, Any]:
    out: dict[str, Any] = {
        "structured_mode": res.structured_mode,
        "finish_reason": res.finish_reason,
        "request_id": res.request_id,
        "notes": res.notes,
    }
    if res.first_token_ms is not None:  # streamed calls: time to the first text piece (S1)
        out["first_token_ms"] = res.first_token_ms
    raw = json.dumps(res.data, ensure_ascii=False) if res.data is not None else None
    if raw is not None and len(raw) <= MAX_RESPONSE_CHARS:
        out["data"] = res.data
    else:
        out["text"] = clip(res.text, MAX_RESPONSE_CHARS)
    if res.parse_error:
        out["parse_error"] = res.parse_error
    return out


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
