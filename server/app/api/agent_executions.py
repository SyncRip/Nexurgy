"""Agent executions (execution-layer design §6.5): detail, timeline (paged and SSE), stop / pause / resume, list,
and the workbench's agent to-dos.

Project members read; stop / pause / resume need the run-control right (project owner or the person who started the
run) and keep the existing lock order. The stream is server-sent events read with fetch (EventSource cannot send the
Authorization header): every event is ``id: <seq>`` / ``event: <type>`` / ``data: <json>``; ``?after=`` or the
``Last-Event-ID`` header resumes; ``event: end`` closes it once the execution finished and every event was sent.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Header
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..db import end_request_session, get_db, new_session
from ..models import AgentExecution, User
from ..security import current_user
from ..services import agent_executions as svc
from ..services import agent_runtime

router = APIRouter(tags=["agents"])

STREAM_POLL_S = 0.5
KEEPALIVE_EVERY = 30  # polls (~15 s)


class StopIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="", max_length=2000)


class PauseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    reason: str = Field(default="", max_length=2000)


class ResumeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


@router.get("/agent-executions")
def list_executions(
    project_id: uuid.UUID | None = None,
    status: str | None = None,
    agent_id: uuid.UUID | None = None,
    runtime: Literal["native", "tool_agent", "workspace_agent"] | None = None,
    role: Literal["execute", "pre_review", "review"] | None = None,
    limit: int = 50,
    cursor: str | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Newest first; ``{items, next_cursor}``. Without project_id: the user's projects only."""
    return svc.list_executions(db, user, project_id=project_id, status=status, agent_id=agent_id, runtime=runtime, role=role, limit=limit,
                               cursor=cursor)


@router.get("/agent-todos")
def agent_todos(project_id: uuid.UUID | None = None, limit: int = 50, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Workbench to-dos from agents: ``agent_question`` and ``budget_exhausted``."""
    return {"items": svc.agent_todos(db, user, project_id=project_id, limit=max(1, min(limit, 200))), "kinds": list(svc.TODO_KINDS)}


@router.get("/agent-executions/{execution_id}")
def get_execution(execution_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    ex, run = svc.load_execution(db, user, execution_id)
    return svc.execution_out(db, user, ex, run)


@router.get("/agent-executions/{execution_id}/events")
def list_events(execution_id: uuid.UUID, after: int = 0, limit: int = 200, user: User = Depends(current_user),
                db: Session = Depends(get_db)) -> dict:
    ex, _ = svc.load_execution(db, user, execution_id)
    rows = svc.events_after(db, ex.id, after, limit)
    cursor = rows[-1].seq if rows else after
    return {"events": [svc.event_out(e) for e in rows], "last_seq": svc.last_seq(db, ex.id), "finished": svc.stream_finished(db, ex, cursor)}


def _resume_from(after: int | None, last_event_id: str | None) -> int:
    if after is not None:
        return max(after, 0)
    if last_event_id and last_event_id.strip().isdigit():
        return int(last_event_id.strip())
    return 0


@router.get("/agent-executions/{execution_id}/stream")
def stream(execution_id: uuid.UUID, after: int | None = None, last_event_id: str | None = Header(default=None),
           user: User = Depends(current_user), db: Session = Depends(get_db)) -> StreamingResponse:
    """The access check uses the request session, closed before the stream starts (no connection held while it is
    open); every poll reads in a short session of its own on a worker thread."""
    svc.load_execution(db, user, execution_id)
    end_request_session(db)
    start = _resume_from(after, last_event_id)

    def poll(cursor: int) -> tuple[list[dict], bool]:
        with new_session() as s:
            rows = svc.events_after(s, execution_id, cursor, 200)
            ex = s.get(AgentExecution, execution_id)
            done = ex is None or (not rows and svc.stream_finished(s, ex, cursor))
            return [svc.event_out(e) for e in rows], done

    async def events():
        cursor = start
        idle = 0
        while True:
            out, done = await run_in_threadpool(poll, cursor)  # sync DB I/O off the event loop
            for e in out:
                cursor = e["seq"]
                yield f"id: {e['seq']}\nevent: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False, default=str)}\n\n"
            if out:
                idle = 0
                continue
            if done:
                yield f"event: end\ndata: {json.dumps({'last_seq': cursor})}\n\n"
                break
            idle += 1
            if idle % KEEPALIVE_EVERY == 0:
                yield ": keep-alive\n\n"
            await asyncio.sleep(STREAM_POLL_S)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/agent-executions/{execution_id}/stop")
def stop(execution_id: uuid.UUID, body: StopIn | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """The node is blocked ``agent_stopped`` (resume requeues it); the running attempt stops at its next check."""
    svc.load_execution(db, user, execution_id)
    agent_runtime.stop_execution(db, user, execution_id, reason=body.reason if body else "")
    db.commit()
    ex, run = svc.load_execution(db, user, execution_id)
    return svc.execution_out(db, user, ex, run)


@router.post("/agent-executions/{execution_id}/pause")
def pause(execution_id: uuid.UUID, body: PauseIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Pauses the execution's run (C06 pause_run; ``expected_version`` is the run's control_version)."""
    ex, _ = svc.load_execution(db, user, execution_id)
    agent_runtime.pause_run(db, user, ex.run_id, expected_version=body.expected_version, reason=body.reason)
    db.commit()
    ex, run = svc.load_execution(db, user, execution_id)
    return svc.execution_out(db, user, ex, run)


@router.post("/agent-executions/{execution_id}/resume")
def resume(execution_id: uuid.UUID, body: ResumeIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Resumes the execution's run (C06 resume_run): a paused run is claimable again and a stopped node is requeued."""
    ex, _ = svc.load_execution(db, user, execution_id)
    agent_runtime.resume_run(db, user, ex.run_id, expected_version=body.expected_version)
    db.commit()
    ex, run = svc.load_execution(db, user, execution_id)
    return svc.execution_out(db, user, ex, run)
