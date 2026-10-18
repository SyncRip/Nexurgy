"""tool_agent executor: Pydantic AI in process (docs/agent/README.zh-CN.md §4.4; execution-layer design §6.1).

Every framework import lives in this file. The rules it keeps:

- Model: native ``GoogleModel`` or ``OpenAIChatModel`` built from the adapter the server resolved from
  the role's frozen snapshot: the address, model id and key come from there (never from OPENAI_* variables), and
  ``provider.check_live()`` (W1 live restrictions) runs before every model request.
- ``strict=False`` on every tool and output tool; flat argument models from the registry; the arguments are checked
  by ``run_tool`` (P2), a bad call comes back to the model as an ``ok=false`` / ``invalid_args`` result.
- ``agent.iter()`` node by node; every model request is streamed (``node.stream``) with the S1 limits (60 s idle after
  the first chunk, the attempt deadline overall). Before every node: ``ctl.alive()``. After every response:
  ``ctl.record_model_call`` (an ai_calls row at once) and the timeline events.
- Limits: ``request_limit`` = max_steps, ``tool_calls_limit`` from the profile options; ``max_total_tokens`` is checked
  here after each response was recorded (the framework's ``total_tokens_limit`` would raise at the end of the stream
  and drop the response that crossed it, already billed); the cost cap is the profile pricing's (checked by
  ``alive()``), never the framework's price table.
- ``deliver`` / ``report_blocked`` are output tools; a delivery is checked with the same ``deliverable_problems`` as the
  native loop and a problem goes back to the model (``ModelRetry``) instead of costing a repair.
- Writing tools run one after another (``sequential``); read-only tools may overlap.
- P4 roles: a review / pre-review attempt finishes with ``submit_review`` (the ReviewReply, checked with the native
  path's ``review_problems``), the AI workspace ask agent with ``final_answer`` (the AskReply), the AI-native main agent
  (``orchestrator``) with ``reply`` (the OrchestratorReply); they come back as ``AttemptResult.output`` and their callers
  validate, record and show them. ``spec.instructions`` / ``spec.user_message`` carry those roles' prompts; model calls
  are recorded under the role's purpose.
- Errors: the native exception behind framework wrappers uses the provider's redacted error mapper.
  A turn cut off without usage counts at its worst case.
- One ``asyncio.run`` per attempt on the worker thread; ``model_factory`` is the seam tests use (``FunctionModel``).
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx2
import pydantic_ai
from google.genai import errors as google_errors
from google.genai.types import HttpRetryOptions
from openai import APIError as OpenAIError, AsyncOpenAI
from pydantic import BaseModel, ValidationError
from pydantic_ai import (
    Agent,
    ModelAPIError,
    ModelHTTPError,
    ModelRequestNode,
    ModelRetry,
    RunContext,
    Tool,
    ToolOutput,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
    UsageLimits,
)
from pydantic_ai.messages import ModelMessagesTypeAdapter, TextPart, ToolCallPart
from pydantic_ai.models import Model
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_graph import End

from ..assistant_agent import MAX_ANSWER_CHARS, AskReply, SuggestedOperation
from ..collaboration_agent import (
    MAX_TOOL_RESULT_CHARS,
    TOOL_AGENT_PROMPT_VERSION,
    TOOL_AGENT_SYSTEM_PROMPT,
    CriterionDecision,
    Deliverable,
    ReviewReply,
    deliverable_problems,
    execute_user_message,
    review_problems,
)
from ..orchestrator_agent import MAX_MESSAGE_CHARS, OrchestratorReply
from ..providers.base import ProviderError, clip, map_transport_error, redact
from ..providers.gemini import map_gemini_error
from ..providers.openai_provider import STREAM_IDLE_TIMEOUT_S, map_openai_error
from ..timeouts import CAD_MODEL_CALL_S, stream_idle_seconds
from ..trace import AgentStep, AiCallRecord
from ..team_prompt import check_prompt_budget, compose_role_prompt, current_team_snapshot
from .base import (
    AttemptControl,
    AttemptResult,
    AttemptSpec,
    AttemptStopped,
    HarnessEvent,
    ToolSpec,
    blocked_questions,
    error_event,
    message_event,
    model_call_payload,
    now_iso,
    safe_args,
    stopped,
    system_event,
    tool_call_record,
    tool_result_payload,
)

pydantic_ai.BANNER_ENABLED = False  # no console banner from the worker
log = logging.getLogger("partroute.tool_agent")
RUNTIME_IMPL = f"pydantic-ai {pydantic_ai.__version__}"
#: Output tokens counted for a turn that was cut off before the server reported usage (and no token cap is set).
WORST_CASE_OUTPUT_TOKENS = 32_768
OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"
TOOL_FAMILIES = ("openai", "openai_compatible", "gemini")

#: Generic contract test seam. Native transport tests leave it unset and intercept HTTP only.
model_factory: Callable[[AttemptSpec], Model] | None = None


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


#: The ``ai_calls.purpose`` of each role's model requests.
PURPOSE_OF_ROLE = {"execute": "agent_execute", "review": "agent_review", "pre_review": "agent_review", "assistant": "assistant",
                   "orchestrator": "assistant"}
#: The output tool each role finishes with (named in the error of a run that ended without it).
END_OF_ROLE = {"review": "submit_review", "pre_review": "submit_review", "assistant": "final_answer", "orchestrator": "reply"}


@dataclass
class _Final:
    kind: str  # deliver | blocked | review | answer | reply
    deliverables: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""
    blocked: dict[str, Any] | None = None
    output: dict[str, Any] | None = None  # review: ReviewReply, answer: AskReply (P4), reply: OrchestratorReply


class _Http:
    """The HTTP side of one attempt's client: the provider request id of the latest response (``x-request-id``)."""

    def __init__(self) -> None:
        self.request_ids: list[str] = []

    async def on_response(self, response: httpx2.Response) -> None:
        rid = response.headers.get("x-request-id")
        if rid:
            self.request_ids.append(rid[:200])

    def take(self, secret: str | None = None) -> str | None:
        rid = self.request_ids[-1] if self.request_ids else None
        self.request_ids.clear()
        return _safe_request_id(rid, secret)


def _safe_request_id(value: str | None, secret: str | None) -> str | None:
    if value and value.isascii() and value.isprintable() and redact(value, (secret,)) == value:
        return value[:200]
    return None


def check_model(provider: Any) -> None:
    """Only the native provider families with an implemented tool transport are accepted."""
    kind = getattr(getattr(provider, "config", None), "provider", None)
    if kind not in TOOL_FAMILIES:
        raise ProviderError("unsupported_capability", f"tool_agent needs an OpenAI, OpenAI-compatible or Gemini model configuration (got {kind})",
                            retryable=False)


@dataclass
class _ModelSession:
    model: Model
    secret: str = field(repr=False)


async def _close_resource(close: Callable[[], Any]) -> None:
    """Close each owned resource even if another close fails; preserve the operation's outcome."""
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except (Exception, asyncio.CancelledError) as exc:
        # SDK exceptions may contain credentials or customer content. Never log their text or traceback here.
        log.warning("model client cleanup failed (%s)", type(exc).__name__)


@asynccontextmanager
async def _provider_model(provider: Any, http: _Http, timeout_s: float,
                          on_request: Callable[[httpx2.Request], Awaitable[None]] | None = None) -> AsyncIterator[_ModelSession]:
    """One explicit frozen configuration and one ownership scope for both attempts and diagnosis."""
    check_model(provider)
    key = provider.resolve_secret()
    cfg = provider.config
    async with AsyncExitStack() as resources:
        try:
            async def before_request(request: httpx2.Request) -> None:
                # Google flattens the client's Timeout into one number. Preserve the existing connect
                # ceiling at the actual HTTP boundary while allowing the longer response/read budget.
                timeout = request.extensions.get("timeout")
                if isinstance(timeout, dict) and timeout.get("connect") is not None:
                    request.extensions["timeout"] = {**timeout, "connect": min(timeout["connect"], timeout_s, 600.0)}
                if on_request is not None:
                    await on_request(request)

            hooks = {"response": [http.on_response], "request": [before_request]}
            # Raise the long response allowance without extending the existing connection ceiling.
            transport_timeout = httpx2.Timeout(timeout_s, connect=min(timeout_s, 600.0))
            client = httpx2.AsyncClient(timeout=transport_timeout, event_hooks=hooks)
            resources.push_async_callback(_close_resource, client.aclose)
            if cfg.provider == "gemini":
                google = GoogleProvider(api_key=key, base_url=cfg.base_url or None, http_client=client,
                                        retry_options=HttpRetryOptions(attempts=1))
                resources.push_async_callback(_close_resource, google.client.close)
                resources.push_async_callback(_close_resource, google.client.aio.aclose)
                model = GoogleModel(cfg.model_id, provider=google)
            else:
                openai = AsyncOpenAI(api_key=key, base_url=cfg.base_url or OPENAI_DEFAULT_BASE_URL,
                                     max_retries=0, timeout=transport_timeout, http_client=client)
                resources.push_async_callback(_close_resource, openai.close)
                model = OpenAIChatModel(cfg.model_id, provider=OpenAIProvider(openai_client=openai))
        except Exception as exc:
            raise _map_error(exc, provider, key) from None
        yield _ModelSession(model, key)


def _map_error(exc: BaseException, provider: Any, secret: str | None) -> ProviderError:
    """Recover native errors through bounded wrapper chains without losing provider semantics."""
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and len(chain) < 12 and all(current is not item for item in chain):
        chain.append(current)
        current = current.__cause__ or current.__context__
    name = getattr(provider, "provider_name", "openai_compatible")
    for cause in chain:
        if isinstance(cause, ProviderError):
            return cause
    if any(isinstance(cause, (UnexpectedModelBehavior, json.JSONDecodeError, ValidationError,
                             google_errors.UnknownApiResponseError)) for cause in chain):
        return ProviderError("bad_response", "the model returned an invalid response", provider=name)
    for cause in chain:
        if name == "gemini" and isinstance(cause, google_errors.APIError):
            return map_gemini_error(cause, secrets=(secret,))
        if name != "gemini" and isinstance(cause, OpenAIError):
            return map_openai_error(cause, provider=name, secrets=(secret,))
    for cause in chain:
        err = map_transport_error(cause, name)
        if err is not None:
            return err
    for cause in chain:
        if isinstance(cause, ModelHTTPError) and name == "gemini":
            body = cause.body if isinstance(cause.body, dict) else {"error": {"message": "model request failed"}}
            err = map_gemini_error(google_errors.APIError(cause.status_code, body), secrets=(secret,))
            err.request_id = _safe_request_id((cause.headers or {}).get("x-request-id"), secret)
            return err
    for cause in chain:
        if isinstance(cause, ValueError):
            return (map_gemini_error(cause, secrets=(secret,)) if name == "gemini"
                    else map_openai_error(cause, provider=name, secrets=(secret,)))
    if any(isinstance(cause, ModelAPIError) for cause in chain):
        return ProviderError("network", "model request failed", provider=name, retryable=True)
    return ProviderError("unknown", f"unexpected model error ({type(exc).__name__})", provider=name)


def _response_error(response: Any) -> ProviderError | None:
    if response.finish_reason in ("content_filter", "length", "error"):
        return ProviderError("bad_response", f"model response cannot execute tools (finish_reason={response.finish_reason})",
                             retryable=False)
    return None


class ToolAgentHarness:
    name = "tool_agent"
    impl = RUNTIME_IMPL

    def run(self, spec: AttemptSpec, ctl: AttemptControl) -> AttemptResult:
        return asyncio.run(_Attempt(spec, ctl).run())


class _Attempt:
    def __init__(self, spec: AttemptSpec, ctl: AttemptControl) -> None:
        self.spec = spec
        self.ctl = ctl
        self.provider = spec.provider
        self.t0 = time.monotonic()
        self.max_steps = int(spec.limits["max_steps"])
        self.deadline = self.t0 + float(spec.limits["max_duration_s"])
        self.state = spec.state
        role = f"\n\nYour role: {clip(spec.role_prompt, 4000)}" if spec.role_prompt else ""
        self.system = (spec.instructions + role) if spec.instructions else TOOL_AGENT_SYSTEM_PROMPT + role
        self.system, self.team = compose_role_prompt(
            self.system, snapshot=current_team_snapshot(), role="manager" if spec.role in ("assistant", "orchestrator") else None)
        self.prompt_version = spec.prompt_version or TOOL_AGENT_PROMPT_VERSION
        self.purpose = PURPOSE_OF_ROLE.get(spec.role, "agent_execute")
        self.produced: dict[str, dict[str, Any]] = {}
        self.tool_calls: list[dict[str, Any]] = []
        self.tool_ai_calls: list[Any] = []
        self.calls: list[AiCallRecord] = []
        self.steps: list[AgentStep] = []
        self.lock = threading.Lock()
        self.secret: str | None = None
        self.http = _Http()
        self.run_ref: str | None = None
        self.tokens_used = 0

    # --- result -----------------------------------------------------------------------------------------------

    def finish(self, status: str, **kw: Any) -> AttemptResult:
        res = AttemptResult(status=status, steps=[s.model_dump(mode="json") for s in self.steps], tool_calls=list(self.tool_calls),
                            calls=list(self.calls), tool_ai_calls=list(self.tool_ai_calls), produced=list(self.produced.values()),
                            duration_ms=int((time.monotonic() - self.t0) * 1000), simulated=False, runtime_impl=RUNTIME_IMPL,
                            runtime_ref=self.run_ref, **kw)
        event = error_event(res.error) if res.error else None
        if event is not None:
            self.ctl.emit(event)
        return res

    def stop(self) -> AttemptResult:
        status, error = stopped(self.ctl)
        if self.ctl.stop_reason == "paused":
            self.ctl.emit(system_event("paused", "the run was paused; the attempt stops before its next step"))
        return self.finish(status, error=error)

    def fail(self, err: ProviderError) -> AttemptResult:
        return self.finish("failed", error={"code": err.code, "message": err.message, "retryable": err.retryable})

    # --- tools ------------------------------------------------------------------------------------------------

    def _tool(self, ts: ToolSpec) -> Tool:
        def call(ctx: RunContext[Any], **kwargs: Any) -> str:
            return self.call_tool(ts, kwargs)

        tool = Tool.from_schema(call, name=ts.name, description=ts.description, json_schema=ts.json_schema(), takes_ctx=True,
                                sequential=not ts.read_only)
        tool.strict = False
        return tool

    def call_tool(self, ts: ToolSpec, raw: dict[str, Any]) -> str:
        """The server tool through the control (lease first, then ``run_tool``: permissions, then the arguments against
        the registry's model — a mismatch comes back as an ``ok=false`` / ``invalid_args`` result the model corrects)."""
        with self.lock:
            index = len(self.tool_calls)
            self.tool_calls.append({})  # reserve the slot (overlapping read-only calls keep their order)
        args = dict(raw or {})
        self.ctl.emit(HarnessEvent(type="tool_call", payload={"call_index": index, "name": ts.name, "args": safe_args(args)}))
        started = time.monotonic()
        res = self.ctl.call_tool(ts.name, args)  # AttemptStopped ends the run (raised through the framework)
        with self.lock:
            self.tool_ai_calls.extend(res.calls)
            self.tool_calls[index] = tool_call_record(ts.name, args, res)
            for p in res.produced:
                self.produced[str(p["asset_version_id"])] = p
            self.state["produced"] = list(self.produced.values())
        self.ctl.emit(HarnessEvent(type="tool_result", payload=tool_result_payload(index, ts.name, res, int((time.monotonic() - started) * 1000))))
        return clip(json.dumps(res.to_dict(), ensure_ascii=False, default=str), MAX_TOOL_RESULT_CHARS)

    def _outputs(self) -> list[ToolOutput]:
        attempt = self
        if self.spec.role in ("review", "pre_review"):
            return [self._review_output()]
        if self.spec.role == "assistant":
            return [self._answer_output()]
        if self.spec.role == "orchestrator":
            return [self._reply_output()]

        def deliver(ctx: RunContext[Any], deliverables: list[Deliverable], note: str = "") -> _Final:
            """Deliver the outputs: one entry per output slot, each a version this attempt produced, a candidate or a bound input."""
            problems = deliverable_problems(deliverables, attempt.state)
            if problems:
                attempt.ctl.emit(system_event("retry", "the delivery was rejected; the model corrects it", problems=problems[:10]))
                attempt.steps.append(AgentStep(kind="repair", ok=False, detail={"problems": [clip(p, 300) for p in problems[:10]]},
                                               started_at=now_iso()))
                raise ModelRetry("The delivery was rejected by the system:\n- " + "\n- ".join(clip(p, 300) for p in problems[:20]))
            items = [{"asset_version_id": str(d.asset_version_id), "slot_key": d.slot_key, "criterion_ids": [str(c) for c in d.criterion_ids],
                      "note": d.note} for d in deliverables]
            return _Final(kind="deliver", deliverables=items, note=clip(note, 4000))

        def report_blocked(ctx: RunContext[Any], reason: str, missing: list[str] | None = None) -> _Final:
            """Report that the task cannot go on: why, and the facts or inputs that are missing (questions for a person)."""
            if not reason.strip():
                raise ModelRetry("give the reason why the task is blocked")
            return _Final(kind="blocked", blocked={"reason": clip(reason, 4000), "missing": [clip(str(m), 2000) for m in (missing or [])][:50]})

        return [ToolOutput(deliver, name="deliver", strict=False), ToolOutput(report_blocked, name="report_blocked", strict=False)]

    def _review_output(self) -> ToolOutput:
        """P4: the verdict of a review / pre-review, checked like the native reply (``review_problems``); a problem goes
        back to the model (``ModelRetry``)."""
        attempt = self

        def submit_review(ctx: RunContext[Any], criteria: list[CriterionDecision], summary: str = "") -> _Final:
            """Give the decision of every criterion (pass, fail or unknown, with the reason and the evidence versions) and a short summary."""
            reply = ReviewReply(criteria=criteria, summary=clip(summary, 8000))
            problems = review_problems(reply, attempt.state)
            if problems:
                attempt.ctl.emit(system_event("retry", "the verdict was rejected; the model corrects it", problems=problems[:10]))
                attempt.steps.append(AgentStep(kind="repair", ok=False, detail={"problems": [clip(p, 300) for p in problems[:10]]},
                                               started_at=now_iso()))
                raise ModelRetry("The verdict was rejected by the system:\n- " + "\n- ".join(clip(p, 300) for p in problems[:20]))
            return _Final(kind="review", output=reply.model_dump(mode="json"))

        return ToolOutput(submit_review, name="submit_review", strict=False)

    def _answer_output(self) -> ToolOutput:
        """P4: the AI workspace answer (AskReply). The agent only explains; a change becomes suggested_operation."""

        def final_answer(ctx: RunContext[Any], answer: str, cited_refs: list[str] | None = None, suggested_operation: SuggestedOperation = "",
                         follow_ups: list[str] | None = None) -> _Final:
            """Give the answer, the keys of the references used, an operation that would make a requested change (or "") and up to 3 follow-up questions."""
            if not answer.strip():
                raise ModelRetry("answer is empty")
            reply = AskReply(answer=clip(answer, MAX_ANSWER_CHARS), cited_refs=[str(k) for k in cited_refs or []][:50],
                             suggested_operation=suggested_operation, follow_ups=[str(f) for f in follow_ups or []][:3])
            return _Final(kind="answer", output=reply.model_dump(mode="json"))

        return ToolOutput(final_answer, name="final_answer", strict=False)

    def _reply_output(self) -> ToolOutput:
        """AI native: the main agent's reply at the end of a turn (what it did, what happens next)."""

        def reply(ctx: RunContext[Any], message: str, suggested_operation: str = "", follow_ups: list[str] | None = None) -> _Final:
            """Tell the person what you did and what happens next, an operation they could start themselves (or "") and up to 3 follow-up requests."""
            if not message.strip():
                raise ModelRetry("message is empty")
            out = OrchestratorReply(message=clip(message, MAX_MESSAGE_CHARS), suggested_operation=str(suggested_operation or ""),
                                    follow_ups=[str(f) for f in follow_ups or []]).cleaned()
            return _Final(kind="reply", output=out.model_dump(mode="json"))

        return ToolOutput(reply, name="reply", strict=False)

    # --- model calls ------------------------------------------------------------------------------------------

    def _request_summary(self, request_no: int, run: Any) -> dict[str, Any]:
        summary = {"runtime": "tool_agent", "request_no": request_no, "messages": len(run.all_messages()),
                   "system_prompt": {"version": self.prompt_version, "sha256": _sha(self.system), "chars": len(self.system)},
                   "tools": [t.name for t in self.spec.tools]}
        if self.team:
            summary["engineering_team"] = self.team
        if getattr(self.provider.config, "provider", None) != "gemini":
            summary["parallel_tool_calls"] = bool(self.spec.runtime_options.get("parallel_tool_calls", True))
        return summary

    def _record(self, call: AiCallRecord) -> str | None:
        self.calls.append(call)
        call_id = self.ctl.record_model_call(call)
        self.ctl.emit(HarnessEvent(type="model_call", payload=model_call_payload(call, call_id)))
        self.steps.append(AgentStep(kind="model_call", ok=call.status == "ok", started_at=call.created_at, duration_ms=call.latency_ms or 0,
                                    detail={"purpose": call.purpose, "provider": call.provider, "model_id": call.model_id,
                                            "tokens_in": call.tokens_in, "tokens_out": call.tokens_out, "error": call.error_code}))
        return call_id

    def _base_call(self, request: dict[str, Any], created_at: str) -> dict[str, Any]:
        p = self.provider
        return {"model_config_id": getattr(p, "config_id", None), "provider": getattr(p, "provider_name", "openai_compatible"),
                "purpose": self.purpose, "request": request, "created_at": created_at, "simulated": False}

    def _worst_case(self, run: Any) -> tuple[int, int]:
        try:
            tokens_in = len(ModelMessagesTypeAdapter.dump_json(run.all_messages())) // 4 + len(self.system) // 4
        except Exception:  # noqa: BLE001 - an estimate only
            tokens_in = len(self.system) // 4
        cap = self.spec.limits.get("max_total_tokens")
        tokens_out = max(int(cap) - self.tokens_used - tokens_in, 0) if cap else WORST_CASE_OUTPUT_TOKENS
        return tokens_in, tokens_out or WORST_CASE_OUTPUT_TOKENS

    async def _model_request(self, node: ModelRequestNode, run: Any, request_no: int) -> AttemptResult | None:
        """Stream one model request; record it. Returns a final result when the attempt ends here."""
        budget = None
        if self.team:
            # Include the current request as well as completed history. The current node's
            # request is not appended to history until the request actually starts.
            messages = list(run.all_messages())
            if not messages or messages[-1] != node.request:
                messages.append(node.request)
            # Pydantic AI also stores the instructions on ModelRequest; the wire
            # sends our system once, so exclude that bookkeeping copy here.
            content = ModelMessagesTypeAdapter.dump_json(messages, exclude={"__all__": {"instructions"}}).decode()
            schema = {"tools": [{"name": t.name, "description": t.description, "parameters": t.json_schema()}
                                for t in self.spec.tools],
                      "output": (AskReply if self.spec.role == "assistant" else OrchestratorReply).model_json_schema()}
            try:
                budget = check_prompt_budget(self.provider.config, self.system, content, schema)
            except ProviderError as exc:
                return self.fail(exc)  # no request, usage row or model-call budget consumed
        request = self._request_summary(request_no, run)
        if budget is not None:
            request["context_budget"] = budget
        created_at = now_iso()
        started = time.monotonic()
        loop = asyncio.get_running_loop()
        call_timeout = max(0.1, min(CAD_MODEL_CALL_S, self.deadline - time.monotonic()))
        abs_deadline = loop.time() + call_timeout
        idle_s = stream_idle_seconds(call_timeout, getattr(self.provider, "stream_idle_timeout_s", None),
                                     short_idle_s=STREAM_IDLE_TIMEOUT_S)
        first_ms: int | None = None
        opened = False
        next_control_check = loop.time()
        self.http.take()  # a failed prior request must not leak its id into this one
        try:
            async with asyncio.timeout(abs_deadline - loop.time()) as tm:
                async with node.stream(run.ctx) as stream:
                    opened = True
                    async for _event in stream:
                        if first_ms is None:
                            first_ms = int((time.monotonic() - started) * 1000)
                        tm.reschedule(min(loop.time() + idle_s, abs_deadline))
                        if loop.time() >= next_control_check:
                            next_control_check = loop.time() + 1.0
                            if not self.ctl.alive():
                                raise AttemptStopped()
                    resp = stream.response
        except UsageLimitExceeded as exc:
            return self._limit(exc)
        except TimeoutError:
            total = loop.time() >= abs_deadline - 0.05
            message = f"the model request exceeded its {call_timeout:g} s time limit" if total else f"the model stream stalled for {idle_s:g} s"
            self._interrupted(run, request, created_at, started, "timeout", message, worst=True)  # the request was sent
            return self.finish("failed", error={"code": "timeout", "message": message, "retryable": True})
        except (UnexpectedModelBehavior, json.JSONDecodeError, ValidationError, google_errors.UnknownApiResponseError):
            message = "the model returned an invalid or incomplete response"
            self._interrupted(run, request, created_at, started, "bad_response", message, worst=True)
            return self.finish("failed", error={"code": "protocol_error", "message": message})
        except AttemptStopped:
            self._interrupted(run, request, created_at, started, "cancelled", "the request was stopped", worst=True)
            return self.stop()
        except asyncio.CancelledError:
            self._interrupted(run, request, created_at, started, "cancelled", "the request was cancelled", worst=True)
            raise
        except Exception as exc:
            err = _map_error(exc, self.provider, self.secret)
            rid = self.http.take(self.secret) or _safe_request_id(err.request_id, self.secret)
            if opened and first_ms is not None:  # cut off mid-stream: no usage came back
                self._interrupted(run, request, created_at, started, err.code, err.message, worst=True, request_id=rid)
            else:
                self._record(AiCallRecord(**self._base_call(request, created_at), model_id=getattr(self.provider, "model_id", "unknown"),
                                          response=None, status="error", error=f"{err.code}: {clip(err.message, 500)}", error_code=err.code,
                                          latency_ms=int((time.monotonic() - started) * 1000), request_id=rid))
            return self.fail(err)
        usage = resp.usage
        tokens_in = int(usage.input_tokens or 0)
        tokens_out = int(usage.output_tokens or 0)
        known = tokens_in > 0 or tokens_out > 0  # no usage reported: unknown, never 0
        self.tokens_used += tokens_in + tokens_out
        text = "".join(p.content for p in resp.parts if isinstance(p, TextPart))
        tool_parts = [p for p in resp.parts if isinstance(p, ToolCallPart)]
        response = {"finish_reason": resp.finish_reason,
                    "request_id": self.http.take(self.secret) or _safe_request_id(resp.provider_response_id, self.secret),
                    "first_token_ms": first_ms, "text": clip(text, 4000),
                    "tool_calls": [{"name": p.tool_name, "args": clip(p.args_as_json_str(), 2000)} for p in tool_parts]}
        rejected = _response_error(resp)
        call = AiCallRecord(**self._base_call(request, created_at), model_id=resp.model_name or getattr(self.provider, "model_id", "unknown"),
                            response=response, status="error" if rejected else "ok",
                            error=f"{rejected.code}: {rejected.message}" if rejected else None,
                            error_code=rejected.code if rejected else None, latency_ms=int((time.monotonic() - started) * 1000),
                            tokens_in=tokens_in if known else None, tokens_out=tokens_out if known else None)
        call_id = self._record(call)
        if rejected is not None:
            return self.fail(rejected)
        event = message_event(text, call_id)
        if event is not None:
            self.ctl.emit(event)
        cap = self.spec.limits.get("max_total_tokens")
        if cap and self.tokens_used > int(cap):  # after the record: the response that crossed the cap is counted
            return self.finish("failed", error={"code": "max_tokens_exhausted", "retryable": True,
                                                "message": f"the attempt used {self.tokens_used} tokens (max_total_tokens {int(cap)})"})
        return None

    def _interrupted(self, run: Any, request: dict[str, Any], created_at: str, started: float, code: str, message: str, *, worst: bool,
                     request_id: str | None = None) -> None:
        """A turn cut off after its request was sent, before the server reported usage: counted at its worst case
        (handoff §6 rule 4); ``worst=False`` (nothing was generated) leaves the tokens unknown, never 0."""
        tokens_in, tokens_out = self._worst_case(run) if worst else (None, None)
        self._record(AiCallRecord(**self._base_call(request, created_at), model_id=getattr(self.provider, "model_id", "unknown"),
                                  response={"interrupted": True, "worst_case": worst}, status="error", error=f"{code}: {clip(message, 500)}",
                                  error_code=code, latency_ms=int((time.monotonic() - started) * 1000), tokens_in=tokens_in,
                                  tokens_out=tokens_out, request_id=request_id or self.http.take(self.secret)))

    def _limit(self, exc: UsageLimitExceeded) -> AttemptResult:
        text = str(exc)
        if "request_limit" in text:
            return self.finish("failed", error={"code": "max_steps_exhausted", "message": f"no delivery after {self.max_steps} steps", "retryable": True})
        if "tool_calls_limit" in text:
            return self.finish("failed", error={"code": "max_tool_calls_exhausted", "message": clip(text, 300), "retryable": True})
        return self.finish("failed", error={"code": "max_tokens_exhausted", "message": clip(text, 300), "retryable": True})

    # --- the run ----------------------------------------------------------------------------------------------

    async def run(self) -> AttemptResult:
        spec, ctl = self.spec, self.ctl
        opts = spec.runtime_options
        try:
            async with AsyncExitStack() as resources:
                try:
                    if model_factory is not None:
                        model = model_factory(spec)
                    else:
                        timeout_s = max(0.1, min(float(spec.limits["max_duration_s"]), CAD_MODEL_CALL_S))
                        session = await resources.enter_async_context(_provider_model(self.provider, self.http, timeout_s))
                        model, self.secret = session.model, session.secret
                    model_settings = ({} if getattr(self.provider.config, "provider", None) == "gemini" else
                                      {"parallel_tool_calls": bool(opts.get("parallel_tool_calls", True))})
                    agent = Agent(model, instructions=self.system, tools=[self._tool(t) for t in spec.tools], output_type=self._outputs(),
                                  retries={"tools": self.max_steps, "output": self.max_steps}, model_settings=model_settings)
                    agent.instrument = False
                except Exception as exc:
                    return self.fail(_map_error(exc, self.provider, self.secret))
                limits = UsageLimits(request_limit=self.max_steps, tool_calls_limit=opts.get("max_tool_calls") or None)
                ctl.emit(system_event("started", "the attempt started", runtime="tool_agent", impl=RUNTIME_IMPL, max_steps=self.max_steps,
                                      **({"role": spec.role} if spec.role != "execute" else {})))
                request_no = 0
                first_message = spec.user_message if spec.user_message is not None else execute_user_message(spec.context, self.state)
                async with agent.iter(first_message, usage_limits=limits) as run:
                    self.run_ref = str(run.run_id)[:200]
                    node = run.next_node
                    while not isinstance(node, End):
                        # The main agent may have durably handed work to an asynchronous child. Its control verifies
                        # live ownership/state before ending; no summarization request (or fresh budget) is needed.
                        handoff = getattr(ctl, "handoff_reply", None) if spec.role == "orchestrator" else None
                        if isinstance(node, ModelRequestNode) and handoff is not None:
                            output = handoff()
                            if output is not None:
                                return self.finish("delivered", output=output)
                        if not ctl.alive():
                            return self.stop()
                        if isinstance(node, ModelRequestNode):
                            self.provider.check_live()  # W1: a disabled config / cleared or revoked key stops here
                            request_no += 1
                            done = await self._model_request(node, run, request_no)
                            if done is not None:
                                return done
                        node = await run.next(node)
                    final = run.result.output if run.result is not None else None
        except AttemptStopped:
            return self.stop()
        except UsageLimitExceeded as exc:
            return self._limit(exc)
        except UnexpectedModelBehavior:
            return self.finish("failed", error={"code": "protocol_error", "message": "the model returned an invalid tool response"})
        except Exception as exc:
            return self.fail(_map_error(exc, self.provider, self.secret))
        if not isinstance(final, _Final):
            ends = END_OF_ROLE.get(spec.role, "deliver or report_blocked")
            return self.finish("failed", error={"code": "protocol_error", "message": f"the run ended without {ends}"})
        if final.kind in ("review", "answer", "reply"):  # the caller validates, records and emits the result (verdict / answer / reply)
            return self.finish("delivered", output=final.output)
        if final.kind == "blocked":
            ctl.emit(HarnessEvent(type="blocked", payload=final.blocked or {}))
            questions = blocked_questions(self.tool_calls, final.blocked)
            if questions:
                ctl.emit(HarnessEvent(type="question", payload={"questions": questions}))
            return self.finish("blocked", blocked=final.blocked, note="")
        ctl.emit(HarnessEvent(type="deliver", payload={"deliverables": final.deliverables, "note": final.note}))
        return self.finish("delivered", deliverables=final.deliverables, note=final.note)


# --- capability diagnosis (POST /ai/model-configs/{id}/diagnose) -------------------------------------------------


class _ProbeArgs(BaseModel):
    city: str


def diagnose(provider: Any, *, timeout_s: float = 60.0) -> dict[str, Any]:
    """Live capability checks through the production native factory (2–3 requests): tool calling,
    parallel tool calls, no ``strict`` in the request, usage fields, streaming. Returns ``{ok, checks, requests}``;
    ``ok`` is the tool-calling check (it decides ``capabilities.tools``)."""
    return asyncio.run(_diagnose(provider, timeout_s))


async def _diagnose(provider: Any, timeout_s: float) -> dict[str, Any]:
    checks: dict[str, dict[str, Any]] = {k: {"ok": False} for k in ("tool_calling", "parallel_tool_calls", "no_strict", "usage", "streaming")}
    bodies: list[dict[str, Any]] = []
    responses: list[Any] = []
    streaming_requests: list[bool] = []
    deltas = 0
    tool_runs: list[str] = []
    final_called = False
    key: str | None = None
    google = getattr(provider.config, "provider", None) == "gemini"

    async def on_request(request: httpx2.Request) -> None:
        try:
            body = json.loads(request.content or b"{}")
            bodies.append(body)
            streaming_requests.append((request.url.path.endswith(":streamGenerateContent") and request.url.params.get("alt") == "sse")
                                      if google else body.get("stream") is True)
        except ValueError:
            pass

    def weather(ctx: RunContext[Any], **kw: Any) -> str:
        tool_runs.append(str(kw.get("city")))
        return json.dumps({"city": kw.get("city"), "temperature_c": 21})

    def answer(ctx: RunContext[Any], summary: str) -> str:
        """Final answer."""
        nonlocal final_called
        final_called = True
        return summary

    tool = Tool.from_schema(weather, name="get_weather", description="Current weather of one city", json_schema=_ProbeArgs.model_json_schema(),
                            takes_ctx=True)
    tool.strict = False
    error: dict[str, Any] | None = None
    try:
        async with _provider_model(provider, _Http(), timeout_s, on_request) as session:
            key = session.secret
            agent = Agent(session.model, tools=[tool], output_type=[ToolOutput(answer, name="final_answer", strict=False)],
                          model_settings={} if google else {"parallel_tool_calls": True},
                          instructions="You are a capability probe. Call get_weather for Paris and for Tokyo in ONE turn (two tool calls), "
                                       "then call final_answer with one sentence.")
            agent.instrument = False
            async with asyncio.timeout(timeout_s * 3):
                async with agent.iter("Weather in Paris and Tokyo?", usage_limits=UsageLimits(request_limit=3)) as run:
                    node = run.next_node
                    while not isinstance(node, End):
                        if isinstance(node, ModelRequestNode):
                            provider.check_live()
                            async with node.stream(run.ctx) as stream:
                                async for _ in stream:
                                    deltas += 1
                                responses.append(stream.response)
                                rejected = _response_error(stream.response)
                                if rejected is not None:
                                    raise rejected
                        node = await run.next(node)
    except (UsageLimitExceeded, UnexpectedModelBehavior, json.JSONDecodeError, ValidationError, google_errors.UnknownApiResponseError):
        error = {"code": "bad_response", "message": "the model returned an invalid or incomplete probe response"}
    except Exception as exc:
        error = _map_error(exc, provider, key).to_dict()
    tool_bodies = [b for b in bodies if b.get("tools")]
    per_response = [sum(1 for p in r.parts if isinstance(p, ToolCallPart)) for r in responses]
    replayed = not google or any("functionResponse" in part for body in bodies for content in body.get("contents", [])
                                 for part in content.get("parts", []))
    checks["tool_calling"] = {"ok": bool(tool_runs) and final_called and replayed and error is None, "tool_runs": len(tool_runs)}
    checks["parallel_tool_calls"] = {"ok": any(n >= 2 for n in per_response), "max_calls_per_response": max(per_response, default=0)}
    if google:
        declarations = [d for b in tool_bodies for t in b["tools"] for d in t.get("functionDeclarations", [])]
        checks["no_strict"] = {"ok": bool(declarations) and not any(d.get("strict") for d in declarations)
                               and not any(b.get("toolConfig", {}).get("functionCallingConfig", {}).get("mode") == "VALIDATED"
                                           for b in tool_bodies)}
    else:
        checks["no_strict"] = {"ok": bool(tool_bodies) and not any(t.get("function", {}).get("strict") for b in tool_bodies for t in b["tools"])}
    checks["usage"] = {"ok": bool(responses) and all((r.usage.input_tokens or 0) > 0 and (r.usage.output_tokens or 0) > 0 for r in responses)}
    checks["streaming"] = {"ok": bool(streaming_requests) and all(streaming_requests) and deltas > 0}
    out: dict[str, Any] = {"ok": checks["tool_calling"]["ok"], "checks": checks, "requests": len(bodies),
                           "model_reported": responses[-1].model_name if responses else None}
    if error:
        out["error"] = error
    return out
