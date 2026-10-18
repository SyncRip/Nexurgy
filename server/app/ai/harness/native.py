"""The native executor: the existing ``ExecuteSession`` loop (one structured reply per step: a tool call, a delivery
or a blocked report), unchanged in prompts, repairs (``MAX_REPAIRS``), step limit and error codes. It only adds the
two P1 duties of every harness: each model call is recorded at once (``ctl.record_model_call``) and the key moments
go to the timeline (``ctl.emit``). Simulation mode always runs this executor."""

from __future__ import annotations

import time
from typing import Any

from ..collaboration_agent import ExecuteSession, deliverable_problems
from ..providers.base import ProviderError
from .base import (
    AttemptControl,
    AttemptResult,
    AttemptSpec,
    AttemptStopped,
    HarnessEvent,
    blocked_questions,
    error_event,
    message_event,
    model_call_payload,
    safe_args,
    stopped,
    system_event,
    tool_call_record,
    tool_result_payload,
)

MAX_REPAIRS = 1
RUNTIME_IMPL = "native"


class NativeHarness:
    name = "native"
    impl = RUNTIME_IMPL

    def run(self, spec: AttemptSpec, ctl: AttemptControl) -> AttemptResult:
        provider = spec.provider
        t0 = time.monotonic()
        max_steps = int(spec.limits["max_steps"])
        deadline = t0 + float(spec.limits["max_duration_s"])
        allowed = {t.name for t in spec.tools}
        state = spec.state
        session = ExecuteSession(provider, role_prompt=spec.role_prompt)
        session.start(spec.context, state)
        produced: dict[str, dict[str, Any]] = {}
        tool_calls: list[dict[str, Any]] = []
        tool_ai_calls: list[Any] = []
        recorded = 0
        repairs = 0
        steps = 0

        def record_new_calls() -> None:
            nonlocal recorded
            for call in session.tracer.calls[recorded:]:
                call_id = ctl.record_model_call(call)
                ctl.emit(HarnessEvent(type="model_call", payload=model_call_payload(call, call_id)))
            recorded = len(session.tracer.calls)

        def finish(status: str, **kw: Any) -> AttemptResult:
            res = AttemptResult(status=status, steps=[s.model_dump(mode="json") for s in session.tracer.steps], tool_calls=tool_calls,
                                calls=list(session.tracer.calls), tool_ai_calls=list(tool_ai_calls),
                                duration_ms=int((time.monotonic() - t0) * 1000), simulated=provider.simulated,
                                produced=list(produced.values()), runtime_impl=RUNTIME_IMPL, **kw)
            event = error_event(res.error) if res.error else None
            if event is not None:
                ctl.emit(event)
            return res

        def stop() -> AttemptResult:
            status, error = stopped(ctl)
            if ctl.stop_reason == "paused":
                ctl.emit(system_event("paused", "the run was paused; the attempt stops before its next step"))
            return finish(status, error=error)

        ctl.emit(system_event("started", "the attempt started", runtime="native", max_steps=max_steps))
        while steps < max_steps:
            if not ctl.alive():
                return stop()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return finish("failed", error={"code": "timeout", "message": "the attempt exceeded max_duration_s", "retryable": True})
            steps += 1
            try:
                reply, problems = session.next_reply(timeout_s=max(5.0, min(remaining, 600.0)))
            except ProviderError as exc:
                record_new_calls()
                return finish("failed", error={"code": exc.code, "message": exc.message, "retryable": exc.retryable})
            record_new_calls()
            if reply is None:
                if repairs < MAX_REPAIRS:
                    repairs += 1
                    session.add_repair(problems, state)
                    ctl.emit(system_event("retry", "the reply was rejected; the model may correct it once", problems=problems[:10]))
                    continue
                return finish("failed", error={"code": "protocol_error", "message": "; ".join(problems)[:2000], "problems": problems[:20]})
            if reply.note and reply.action == "tool":
                event = message_event(reply.note)
                if event is not None:
                    ctl.emit(event)
            if reply.action == "tool":
                assert reply.tool is not None
                name = reply.tool.name
                if name not in allowed:
                    problems = [f"tool_not_allowed: {name} is not in the task recipe or the agent profile (allowed: {', '.join(sorted(allowed)) or 'none'})"]
                    if repairs < MAX_REPAIRS:
                        repairs += 1
                        session.add_repair(problems, state)
                        ctl.emit(system_event("retry", problems[0], problems=problems))
                        continue
                    return finish("failed", error={"code": "protocol_error", "message": problems[0], "problems": problems})
                index = len(tool_calls)
                ctl.emit(HarnessEvent(type="tool_call", payload={"call_index": index, "name": name, "args": safe_args(reply.tool.args)}))
                started = time.monotonic()
                try:
                    res = ctl.call_tool(name, reply.tool.args)
                except AttemptStopped:
                    return stop()
                tool_ai_calls.extend(res.calls)  # a tool's own model calls count against the node budget too (§3.9)
                tool_calls.append(tool_call_record(name, reply.tool.args, res))
                ctl.emit(HarnessEvent(type="tool_result", payload=tool_result_payload(index, name, res, int((time.monotonic() - started) * 1000))))
                for p in res.produced:
                    produced[str(p["asset_version_id"])] = p
                state["produced"] = list(produced.values())
                session.add_tool_result(name, res.to_dict(), state)
                continue
            if reply.action == "blocked":
                assert reply.blocked is not None
                blocked = {"reason": reply.blocked.reason, "missing": reply.blocked.missing}
                ctl.emit(HarnessEvent(type="blocked", payload=blocked))
                questions = blocked_questions(tool_calls, blocked)
                if questions:
                    ctl.emit(HarnessEvent(type="question", payload={"questions": questions}))
                return finish("blocked", blocked=blocked, note=reply.note)
            assert reply.deliverables is not None
            problems = deliverable_problems(reply.deliverables, state)
            if problems:
                if repairs < MAX_REPAIRS:
                    repairs += 1
                    session.add_repair(problems, state)
                    ctl.emit(system_event("retry", "the delivery was rejected; the model may correct it once", problems=problems[:10]))
                    continue
                return finish("failed", error={"code": "protocol_error", "message": "; ".join(problems)[:2000], "problems": problems[:20]})
            deliverables = [{"asset_version_id": str(d.asset_version_id), "slot_key": d.slot_key, "criterion_ids": [str(c) for c in d.criterion_ids],
                             "note": d.note} for d in reply.deliverables]
            ctl.emit(HarnessEvent(type="deliver", payload={"deliverables": deliverables, "note": reply.note or ""}))
            return finish("delivered", note=reply.note or "", deliverables=deliverables)
        return finish("failed", error={"code": "max_steps_exhausted", "message": f"no delivery after {max_steps} steps", "retryable": True})
