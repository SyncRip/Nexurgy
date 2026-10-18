"""Execution interface of one agent attempt (docs/agent/README.zh-CN.md §4.2; execution-layer design §6.1).

The control plane (``services/agent_runtime.py``: queue, lease, budget, write-back, review) stays the only source of
truth. A harness runs ONE claimed attempt and talks to it only through plain data (:class:`AttemptSpec`) and the
callbacks of :class:`AttemptControl`; nothing here imports ``app.models`` or opens a database session.

- ``alive()``: lease, profile, run state, stop request, duration, steps / tool calls / cost; ``stop_reason`` says why not.
- ``call_tool(name, args)``: the server-side tool (permissions checked there); raises :class:`AttemptStopped` when the
  attempt may not go on.
- ``record_model_call(call)``: one ``ai_calls`` row now (own transaction), counted in the attempt's budget.
- ``emit(event)``: one timeline event under the lease.

:class:`AttemptResult` mirrors the runtime's ``_Outcome`` so ``_write_back_execute`` and below stay unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from ..providers.base import clip

Runtime = Literal["native", "tool_agent"]
SUPPORTED_RUNTIMES: tuple[str, ...] = ("native", "tool_agent")
EventType = Literal["system", "model_call", "message", "tool_call", "tool_result", "question", "deliver", "blocked", "verdict", "status",
                    "command", "file_change", "confirmation"]  # retained for historical workspace executions
MAX_MESSAGE_EVENT_CHARS = 4_000
MAX_ARG_CHARS = 2_000

#: Why ``alive()`` said no. The harness maps them onto the outcome the write-back already knows (see :func:`stopped`).
StopReason = Literal["lease_lost", "stopped", "disabled", "cancelled", "paused", "timeout", "max_steps", "max_tool_calls",
                     "budget_exceeded"]


#: ``write``: changes data (runs one after the other); ``read``: may overlap; ``review_only``: only review / general
#: profiles hold it; ``end``: ``deliver`` / ``report_blocked``, which finish an attempt.
ToolKind = Literal["write", "read", "review_only", "end"]


class ToolSpec(BaseModel):
    """One tool as an executor sees it (registry: ``app/ai/tool_registry.py``): description and argument model only,
    no execution. Flat argument models use no discriminated unions. ``native_note`` /
    ``native_omit``: how the native prompt's tool rule describes it (tools without a note are not described there)."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    name: str
    description: str
    args_model: type[BaseModel]
    kind: ToolKind = "write"
    needs_part: bool = False
    produces_assets: bool = False
    native_note: str = ""
    native_omit: tuple[str, ...] = ()

    @property
    def read_only(self) -> bool:
        return self.kind == "read"

    @property
    def review_only(self) -> bool:
        return self.kind == "review_only"

    def json_schema(self) -> dict[str, Any]:
        return self.args_model.model_json_schema()

    def contract(self) -> dict[str, Any]:
        """The public contract (``GET /agent-tools``, the profile editor's tool contract)."""
        return {"name": self.name, "description": self.description, "kind": self.kind, "needs_part": self.needs_part,
                "produces_assets": self.produces_assets, "args_schema": self.json_schema()}


class HarnessEvent(BaseModel):
    """A timeline event (design §6.4). The control adds ``simulated`` and the sequence number."""

    model_config = ConfigDict(extra="forbid")

    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)


class AttemptSpec(BaseModel):
    """Everything one attempt needs, as plain data. ``provider`` is the adapter the server resolved from the role's
    frozen model snapshot (key bound, live guard bound); ``tool_agent`` takes its address and key from it."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # P4: review / pre-review attempts finish with the ReviewReply output tool, the AI workspace ask agent ("assistant")
    # with the AskReply one; execute attempts with deliver / report_blocked. AI native: a main-agent turn
    # ("orchestrator") finishes with reply (OrchestratorReply).
    role: Literal["execute", "pre_review", "review", "assistant", "orchestrator"] = "execute"
    runtime: Runtime = "native"
    runtime_options: dict[str, Any] = Field(default_factory=dict)
    role_prompt: str = ""
    context: dict[str, Any] = Field(default_factory=dict)  # task, demand, recipe, rework feedback (execute_user_message)
    state: dict[str, Any] = Field(default_factory=dict)  # slots, bound inputs, candidates, criteria, clarifications
    tools: list[ToolSpec] = Field(default_factory=list)  # the allowed tools only (recipe ∩ profile, never review-only)
    limits: dict[str, Any] = Field(default_factory=dict)  # max_steps, max_duration_s, max_tool_calls?, max_total_tokens?
    provider: Any = Field(default=None, exclude=True)
    simulated: bool = False
    # P4: a role's own prompts (review / assistant); None = the execute protocol's (TOOL_AGENT_SYSTEM_PROMPT + role
    # prompt, execute_user_message).
    instructions: str | None = None
    user_message: str | None = None
    prompt_version: str | None = None


class AttemptResult(BaseModel):
    """Aligned with ``agent_runtime._Outcome`` (+ runtime_impl / runtime_ref). ``calls`` are the attempt's own model
    calls (already stored by ``record_model_call``), ``tool_ai_calls`` the calls made inside tools."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    status: Literal["delivered", "blocked", "failed", "superseded", "paused"]
    deliverables: list[dict[str, Any]] = Field(default_factory=list)
    note: str = ""
    blocked: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    steps: list[dict[str, Any]] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    calls: list[Any] = Field(default_factory=list)
    tool_ai_calls: list[Any] = Field(default_factory=list)
    produced: list[dict[str, Any]] = Field(default_factory=list)
    duration_ms: int = 0
    simulated: bool = False
    runtime_impl: str | None = None
    runtime_ref: str | None = None
    # P4: the final output of a review (ReviewReply), an ask (AskReply) or a main-agent turn (OrchestratorReply) attempt,
    # as plain data (status delivered).
    output: dict[str, Any] | None = None


class AttemptStopped(Exception):
    """Raised by ``AttemptControl.call_tool`` when the attempt may not go on (``ctl.stop_reason`` says why)."""


@runtime_checkable
class ToolResultLike(Protocol):
    ok: bool
    tool: str
    message: str
    produced: list[dict[str, Any]]
    data: dict[str, Any]
    error: dict[str, Any] | None
    simulated: bool
    calls: list[Any]

    def to_dict(self) -> dict[str, Any]: ...


class AttemptControl(Protocol):
    """Implemented by ``services/agent_runtime.py`` (design §6.2)."""

    stop_reason: StopReason | None

    def alive(self) -> bool: ...

    def call_tool(self, name: str, args: dict[str, Any]) -> ToolResultLike: ...

    def record_model_call(self, call: Any) -> str | None: ...

    def emit(self, event: HarnessEvent) -> None: ...


class AgentHarness(Protocol):
    name: str
    impl: str

    def run(self, spec: AttemptSpec, ctl: AttemptControl) -> AttemptResult: ...


# --- helpers shared by the harnesses -------------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def safe_args(args: dict[str, Any]) -> dict[str, Any]:
    """Tool arguments as recorded in steps and events (long strings clipped)."""
    out = {}
    for k, v in (args or {}).items():
        out[k] = clip(v, MAX_ARG_CHARS) if isinstance(v, str) else v
    return out


def tool_call_record(name: str, args: dict[str, Any], res: ToolResultLike) -> dict[str, Any]:
    """One entry of ``agent_executions.tool_calls`` (the same shape for every executor). A read-only evidence tool's
    locator of what it read (``data.evidence``, P4) is kept as ``evidence``."""
    rec = {"name": name, "args": safe_args(args), "ok": res.ok, "message": res.message, "produced": res.produced,
           "error": res.error, "at": now_iso(), "simulated": res.simulated, "ai_calls": len(res.calls),
           "questions": list((res.data or {}).get("questions") or [])[:10]}
    evidence = (res.data or {}).get("evidence") if isinstance(res.data, dict) else None
    if isinstance(evidence, dict):
        rec["evidence"] = evidence
    # generation reliability N: the business checks the call really ran / skipped (a delivery counts them for its run)
    execution = (res.data or {}).get("execution") if isinstance(res.data, dict) else None
    if isinstance(execution, dict) and isinstance(execution.get("checks"), dict):
        rec["checks_ledger"] = execution["checks"]
    return rec


def tool_result_payload(index: int, name: str, res: ToolResultLike, duration_ms: int) -> dict[str, Any]:
    out = {"call_index": index, "name": name, "ok": res.ok, "message": clip(res.message, 2000), "error": res.error,
           "produced": [{"asset_version_id": p.get("asset_version_id"), "asset_type": p.get("asset_type"), "label": p.get("label")}
                        for p in res.produced],
           "duration_ms": duration_ms, "ai_calls": len(res.calls)}
    # generation reliability N: how the call ran (policy and its source, inputs, target, budget, checks) — the timeline shows it
    execution = (res.data or {}).get("execution") if isinstance(res.data, dict) else None
    if isinstance(execution, dict):
        out["execution"] = execution
    return out


def model_call_payload(call: Any, call_id: str | None) -> dict[str, Any]:
    data = call.model_dump() if hasattr(call, "model_dump") else dict(call)
    response = data.get("response") or {}
    out = {"call_id": call_id, "model_id": data.get("model_id"), "tokens_in": data.get("tokens_in"), "tokens_out": data.get("tokens_out"),
           "latency_ms": data.get("latency_ms"), "finish_reason": response.get("finish_reason"), "status": data.get("status"),
           "simulated": bool(data.get("simulated"))}
    if response.get("first_token_ms") is not None:
        out["first_token_ms"] = response["first_token_ms"]
    if data.get("error_code"):
        out["error_code"] = data["error_code"]
    return out


def blocked_questions(tool_calls: list[dict[str, Any]], blocked: dict[str, Any] | None) -> list[str]:
    """What a person is asked (same rule as the runtime's blocked reason): the CAD tools' clarification questions of
    this attempt, else the agent's list of missing facts."""
    qs: list[str] = []
    for tc in tool_calls:
        if (tc.get("error") or {}).get("code") == "needs_clarification":
            qs += [str(q).strip() for q in tc.get("questions") or [] if str(q).strip()]
    if not qs:
        qs = [str(m).strip() for m in (blocked or {}).get("missing") or [] if str(m).strip()]
    return list(dict.fromkeys(qs))[:20]


def stopped(ctl: AttemptControl) -> tuple[str, dict[str, Any] | None]:
    """(status, error) of an attempt that ``alive()`` stopped, in the terms the write-back already handles:
    paused → paused (products stay candidates); timeout / per-attempt limits → a failed technical attempt;
    cost cap → failed budget_exceeded (the write-back then blocks the node); a lost lease, a stop request, a disabled
    profile or a cancelled run → superseded (history only, its usage still counted)."""
    reason = ctl.stop_reason
    if reason == "paused":
        return "paused", None
    if reason == "timeout":
        return "failed", {"code": "timeout", "message": "the attempt exceeded max_duration_s", "retryable": True}
    if reason == "max_steps":
        return "failed", {"code": "max_steps_exhausted", "message": "the attempt used up its model requests", "retryable": True}
    if reason == "max_tool_calls":
        return "failed", {"code": "max_tool_calls_exhausted", "message": "the attempt used up its tool calls", "retryable": True}
    if reason == "budget_exceeded":
        return "failed", {"code": "budget_exceeded", "message": "the node's cost cap was reached during the attempt", "retryable": False}
    if reason == "stopped":
        return "superseded", {"code": "agent_stopped", "message": "a person stopped the attempt; its result is history only"}
    return "superseded", None


def system_event(code: str, message: str = "", **detail: Any) -> HarnessEvent:
    payload: dict[str, Any] = {"code": code, "message": message}
    if detail:
        payload["detail"] = detail
    return HarnessEvent(type="system", payload=payload)


#: System codes of design §6.4 that are shown as such; any other error is a ``failed`` event carrying its own code.
OWN_SYSTEM_CODES = frozenset({"timeout", "budget_exceeded"})


def error_event(error: dict[str, Any]) -> HarnessEvent | None:
    """The timeline event of an attempt's error (none for a stop request: the stop itself was already recorded)."""
    code = str(error.get("code") or "failed")
    if code == "agent_stopped":
        return None
    detail = {"error_code": code, "retryable": error.get("retryable")}
    return system_event(code if code in OWN_SYSTEM_CODES else "failed", str(error.get("message") or ""), **detail)


def message_event(text: str, call_id: str | None = None) -> HarnessEvent | None:
    text = (text or "").strip()
    if not text:
        return None
    return HarnessEvent(type="message", payload={"text": clip(text, MAX_MESSAGE_EVENT_CHARS), "call_id": call_id})
