"""Read-only look-ups of the AI workspace ask agent (execution-layer design §9; docs/agent/README.zh-CN.md §5.4).

An ``ask`` operation runs on the ``tool_agent`` executor when its frozen model can call tools (an OpenAI-family
configuration with ``capabilities.tools``), the model is real (simulation always answers with the labelled single
call) and the context build leaves room for look-ups (:func:`ask_runtime`); ``PARTROUTE_ASSISTANT_RUNTIME=native`` is
the rollback. The context build is unchanged: the same private conversation context (memory, pinned constraints,
references) inside the same budget. The agent may then look up more of the conversation's project with the registry's
``ASSISTANT_TOOL_SPECS`` (``project_overview``, ``get_part``, ``get_revision``, ``read_file``, ``search_knowledge``):
each call is one short read transaction inside that project only (``ask`` already needs ``view`` on it), results are
capped so the last request still fits the model's window, and nothing is ever written. What was read is stored on the
answer as ``consulted``. A change request still ends as ``suggested_operation`` — the composer's confirmation card the
person sends — so neither the agent nor its tools change data.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any

from ..ai import evidence
from ..ai.assistant_agent import ASK_TOOL_PROMPT_VERSION, AskOutcome, AskReply, ask_tool_system_prompt, keys_in, outcome_of
from ..ai.harness.base import AttemptSpec, AttemptStopped, HarnessEvent
from ..ai.providers.base import clip
from ..ai.tool_registry import ASSISTANT_TOOL_SPECS, check_args
from ..config import get_settings
from ..db import session_scope
from ..errors import AppError
from .assistant_queries import QUERIES as _TOOLS, consulted_of
from . import feature_flags as features

log = logging.getLogger("partroute.assistant_tools")

#: Model requests of one answer (look-ups, then final_answer) and its look-ups.
MAX_STEPS = 4
MAX_TOOL_CALLS = 6
MAX_DURATION_S = 120
#: Characters of one look-up result: within [MIN, MAX], and all of them together within the room the build left.
RESULT_CHARS_MAX = 6_000
RESULT_CHARS_MIN = 1_000
#: Below this many tokens of room after the build (and the longer prompt), the answer stays a single call.
MIN_ROOM_TOKENS = 2_000


# --- runtime choice ---------------------------------------------------------------------------------------------


def _room_tokens(build: Any) -> int:
    from .assistant_context import estimate_tokens

    budget = build.budget or {}
    extra = max(estimate_tokens(ask_tool_system_prompt()) - estimate_tokens(build.system_text or ""), 0)
    return int(budget.get("available") or 0) - int(budget.get("used_est") or 0) - extra


def ask_runtime(provider: Any, cfg: Any, build: Any) -> str:
    """``tool_agent`` or ``native`` for one ask (see the module doc)."""
    if get_settings().assistant_runtime == "native" or getattr(provider, "simulated", True) or cfg is None:
        return "native"
    from ..ai.harness.tool_agent import TOOL_FAMILIES
    if cfg.provider not in TOOL_FAMILIES or not cfg.capabilities.tools:
        return "native"
    return "tool_agent" if _room_tokens(build) >= MIN_ROOM_TOKENS else "native"


def result_chars(build: Any) -> int:
    """Characters per look-up so that every look-up together stays within the room (~1.5 characters per token)."""
    return max(RESULT_CHARS_MIN, min(RESULT_CHARS_MAX, int(_room_tokens(build) * 1.5) // MAX_TOOL_CALLS))


# --- the look-ups -----------------------------------------------------------------------------------------------



def run_lookup(name: str, args: Any, project_id: uuid.UUID, limit: int = RESULT_CHARS_MAX) -> evidence.EvidenceResult:
    """One look-up in its own read transaction, inside ``project_id`` only; never raises."""
    spec = ASSISTANT_TOOL_SPECS.get(name)
    if spec is None:
        return evidence.fail(name, "unknown_tool", f"{name} is not a look-up tool")
    checked, problems = check_args(spec, args if args is not None else {})
    if checked is None:
        return evidence.EvidenceResult(ok=False, tool=name, message="invalid arguments: " + "; ".join(problems)[:1500],
                                       error={"code": "invalid_args", "message": "the arguments do not match the tool schema", "problems": problems})
    try:
        with session_scope() as db:
            if name == "search_knowledge":
                features.require_enabled("collaboration")
            return _TOOLS[name](db, project_id, checked, limit)
    except AppError as exc:
        return evidence.EvidenceResult(ok=False, tool=name, message=exc.message,
                                       error={"code": exc.code, "message": exc.message, "details": exc.details})
    except Exception as exc:  # noqa: BLE001 - a read problem is a tool result
        log.exception("ask look-up %s failed", name)
        return evidence.fail(name, "tool_error", clip(f"{type(exc).__name__}: {exc}", 300))


# --- the agent --------------------------------------------------------------------------------------------------


class AskControl:
    """``AttemptControl`` of one ask: a deadline, the request and look-up limits; calls are kept for the caller to record
    with the operation (as the single call's are); nothing here writes."""

    def __init__(self, project_id: uuid.UUID, *, limit_chars: int) -> None:
        self.project_id = project_id
        self.limit_chars = limit_chars
        self.stop_reason: str | None = None
        self.deadline = time.monotonic() + MAX_DURATION_S
        self.calls: list[Any] = []
        self.tool_count = 0
        self._lock = threading.Lock()

    def alive(self) -> bool:
        with self._lock:
            calls, tools = len(self.calls), self.tool_count
        if time.monotonic() >= self.deadline:
            self.stop_reason = "timeout"
        elif calls > MAX_STEPS:
            self.stop_reason = "max_steps"
        elif tools > MAX_TOOL_CALLS:
            self.stop_reason = "max_tool_calls"
        else:
            self.stop_reason = None
        return self.stop_reason is None

    def call_tool(self, name: str, args: dict[str, Any]) -> evidence.EvidenceResult:
        if not self.alive():
            raise AttemptStopped(self.stop_reason)
        with self._lock:
            self.tool_count += 1
        return run_lookup(name, args, self.project_id, self.limit_chars)

    def record_model_call(self, call: Any) -> str | None:
        with self._lock:
            self.calls.append(call)
        return None

    def emit(self, event: HarnessEvent) -> None:  # an ask has no timeline; what was read is returned as ``consulted``
        return None


def run_ask_agent(provider: Any, build: Any, *, project_id: uuid.UUID) -> tuple[AskOutcome, list[dict[str, Any]]]:
    """One answer on the tool_agent executor: the build's user message, the ask tool prompt, the look-ups, the AskReply
    output tool. Never raises for model / provider problems (they come back in ``AskOutcome.error``)."""
    from ..ai.harness import get_harness

    ctl = AskControl(project_id, limit_chars=result_chars(build))
    spec = AttemptSpec(role="assistant", runtime="tool_agent", runtime_options={"parallel_tool_calls": True, "max_tool_calls": MAX_TOOL_CALLS},
                       tools=[s for name, s in ASSISTANT_TOOL_SPECS.items()
                              if name != "search_knowledge" or features.enabled("collaboration")], provider=provider,
                       limits={"max_steps": MAX_STEPS, "max_duration_s": MAX_DURATION_S, "max_tool_calls": MAX_TOOL_CALLS},
                       instructions=ask_tool_system_prompt(), user_message=build.context_text, prompt_version=ASK_TOOL_PROMPT_VERSION)
    result = get_harness("tool_agent").run(spec, ctl)  # type: ignore[arg-type]
    consulted = consulted_of(result.tool_calls)
    if result.status == "delivered" and result.output:
        reply = AskReply.model_validate(result.output)
        return outcome_of(reply, set(keys_in(build.context_text)), simulated=False, calls=list(result.calls)), consulted
    error = result.error or {"code": "bad_response", "message": "the model did not give an answer"}
    return AskOutcome(answer="", cited_keys=[], suggested_operation="", follow_ups=[], simulated=False, calls=list(result.calls),
                      error=error), consulted
