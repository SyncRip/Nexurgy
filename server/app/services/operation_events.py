"""Private completion outbox. Domain writes emit; a separate transaction delivers.

Deploy the generic outbox kind allowlist to every worker before enabling this
writer. Old follow-child writers must be stopped during the atomic rollout.
Delivery locks conversation -> outbox only, never a domain/operation row.
"""
from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import AiConversation, AiMessage, AiOperation, OutboxEvent

KIND = "ai.operation.completed"
CLOSED = ("consumed", "suppressed", "delivery_failed", "legacy_consumed")


def _now():
    return datetime.now(UTC)


def _uuid(value):
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


def events_for(db: Session, operation_id: uuid.UUID) -> list[OutboxEvent]:
    return list(db.scalars(select(OutboxEvent).where(OutboxEvent.kind == KIND, OutboxEvent.ai_operation_id == operation_id)
                           .order_by(OutboxEvent.ai_event_seq)))


def public_events(db: Session, operation_id: uuid.UUID) -> list[dict]:
    return [{"event_id": str(e.id), "generation": e.ai_generation, "event_seq": e.ai_event_seq, "phase": e.ai_phase,
             "outcome": e.payload.get("outcome"), "wake_state": e.ai_wake_state, "notice_seq": e.ai_notice_seq}
            for e in events_for(db, operation_id)]


def emit(db: Session, op: AiOperation, *, phase: str = "terminal", outcome: str | None = None) -> OutboxEvent:
    """Freeze exactly once under the operation lock, in the domain transaction."""
    from . import orchestrator

    if phase not in ("terminal", "applied", "undone"):
        raise ValueError("unsupported completion phase")
    db.flush()
    db.execute(select(AiOperation.id).where(AiOperation.id == op.id).with_for_update())
    generation = op.generation
    existing = db.scalar(select(OutboxEvent).where(OutboxEvent.kind == KIND, OutboxEvent.ai_operation_id == op.id,
                                                  OutboxEvent.ai_generation == generation, OutboxEvent.ai_phase == phase))
    if existing is not None:
        return existing
    legacy = (op.result or {}).get("terminal_notification") if generation == 1 and phase == "terminal" else None
    legacy = legacy if isinstance(legacy, dict) else {}
    historical = None
    if generation == 1 and phase == "terminal" and not legacy:
        old_id = uuid.uuid5(op.id, "terminal")
        historical = db.scalar(select(AiMessage).where(AiMessage.conversation_id == op.conversation_id,
                    AiMessage.kind == "notice", AiMessage.payload["event_id"].astext == str(old_id)))
        if historical is not None:
            legacy = {"event_id": str(old_id), "notice_seq": historical.seq}
    eid = _uuid(legacy.get("event_id")) or uuid.uuid5(op.id, f"completion:v1:{generation}:{phase}")
    seq = 1 + int(db.scalar(select(func.max(OutboxEvent.ai_event_seq)).where(OutboxEvent.ai_operation_id == op.id)) or 0)
    req = op.request or {}
    brief = orchestrator.child_brief(op)
    payload = {**copy.deepcopy(brief), "schema_version": 1, "event_id": str(eid), "event_seq": seq, "generation": generation,
               "phase": phase, "outcome": outcome or (phase if phase != "terminal" else brief["outcome"]),
               "parent_operation_id": req.get("parent_op_id"), "root_operation_id": req.get("root_op_id") or str(op.id)}
    notice_seq = legacy.get("notice_seq")
    # A historical notice may have committed before its old marker. Adopt it without duplicating the message.
    notice = db.scalar(select(AiMessage).where(AiMessage.conversation_id == op.conversation_id,
                                             AiMessage.kind == "notice", AiMessage.payload["event_id"].astext == str(eid)))
    if notice is not None:
        notice_seq = notice.seq
        payload.update({k: notice.payload[k] for k in ("status", "outcome") if k in notice.payload})
    if legacy and not legacy.get("wake_turn_id"):
        old_wake = db.scalar(select(AiOperation).where(AiOperation.conversation_id == op.conversation_id,
                                            AiOperation.client_request_id == uuid.uuid5(op.id, "resume")))
        if old_wake is not None:
            legacy = {**legacy, "wake_turn_id": str(old_wake.id), "wake_state": "queued"}
    state = {"coalesced": "legacy_consumed", "queued": "assigned", "suppressed": "suppressed"}.get(legacy.get("wake_state"), "pending")
    ev = OutboxEvent(id=eid, kind=KIND, project_id=op.project_id, payload=payload,
                     dedupe_key=f"{KIND}:{op.id}:{generation}:{phase}", ai_operation_id=op.id, ai_generation=generation,
                     ai_event_seq=seq, ai_phase=phase, ai_wake_state=state, ai_notice_seq=notice_seq,
                     ai_wake_turn_id=_uuid(legacy.get("wake_turn_id")), processed_at=_now() if state in CLOSED else None)
    db.add(ev)
    op.result = {**(op.result or {}), "completion_event": {"event_id": str(eid), "generation": generation, "event_seq": seq, "phase": phase}}
    db.flush()
    return ev


def reopen(db: Session, op: AiOperation) -> None:
    """Call only after the checkpoint retry really succeeded, before clearing result."""
    emit(db, op)
    op.generation += 1


def _close(ev: OutboxEvent, state: str, error: str | None = None) -> None:
    ev.ai_wake_state, ev.processed_at = state, _now()
    ev.last_error = error
    if state == "consumed":
        ev.ai_consumed_at = ev.processed_at


def _context_ids(op: AiOperation) -> set[str]:
    return set((op.result or {}).get("completion_context") or [])


def complete_context(db: Session, op: AiOperation) -> None:
    """Only a committed answer/next human confirmation proves context consumption."""
    if op.status not in ("succeeded", "waiting_input"):
        return
    ids = [_uuid(i) for i in _context_ids(op)]
    if not ids:
        return
    for ev in db.scalars(select(OutboxEvent).where(OutboxEvent.kind == KIND, OutboxEvent.id.in_(ids),
                                                  OutboxEvent.ai_wake_turn_id == op.id).with_for_update()):
        _close(ev, "consumed")


def retry_context(db: Session, previous: AiOperation, retry: AiOperation) -> None:
    """Explicit human retry reassigns unconsumed delivery; the immutable event and its notice stay unchanged.

    Called by the retry API while it holds operation → conversation locks. No automated path invokes this.
    """
    from . import orchestrator

    if not orchestrator.is_turn(previous) or orchestrator.root_of(previous) != orchestrator.root_of(retry):
        return
    for ev in db.scalars(select(OutboxEvent).where(OutboxEvent.kind == KIND, OutboxEvent.ai_wake_turn_id == previous.id,
                            OutboxEvent.ai_wake_state.in_(("assigned", "delivery_failed")), OutboxEvent.ai_consumed_at.is_(None))
                          .with_for_update()):
        ev.ai_wake_turn_id, ev.ai_wake_state = retry.id, "assigned"
        ev.processed_at, ev.last_error = None, None


def _suppressed(db: Session, conv: AiConversation, ev: OutboxEvent) -> bool:
    if conv.archived_at is not None:
        return True
    for key in ("parent_operation_id", "root_operation_id"):
        oid = _uuid(ev.payload.get(key))
        operation = db.get(AiOperation, oid, populate_existing=True) if oid else None
        if operation is None or operation.status == "cancelled":
            return True
    return False


def drain(db: Session, *, limit: int = 50) -> int:
    """Dedicated consumer, called only after the domain synchronization committed."""
    from . import assistant as asst, orchestrator

    if not orchestrator._enabled():
        return 0
    candidates = db.scalars(select(AiOperation.conversation_id).join(OutboxEvent, OutboxEvent.ai_operation_id == AiOperation.id)
                           .where(OutboxEvent.kind == KIND, OutboxEvent.ai_wake_state.in_(("pending", "assigned")))
                           .distinct().limit(limit)).all()
    count = 0
    for cid in candidates:
        conv = db.scalar(select(AiConversation).where(AiConversation.id == cid).with_for_update(skip_locked=True))
        if conv is None:
            continue
        rows = db.scalars(select(OutboxEvent).join(AiOperation, OutboxEvent.ai_operation_id == AiOperation.id)
                          .where(OutboxEvent.kind == KIND, AiOperation.conversation_id == cid,
                                 OutboxEvent.ai_wake_state.in_(("pending", "assigned")))
                          .order_by(OutboxEvent.created_at, OutboxEvent.ai_event_seq, OutboxEvent.id)
                          .with_for_update(of=OutboxEvent, skip_locked=True)).all()
        for ev in rows:
            if _suppressed(db, conv, ev):
                _close(ev, "suppressed")
                continue
            if ev.ai_notice_seq is None:
                msg = asst._append(db, conv, role="assistant", kind="notice", payload={"event": "sub_agent_finished", **ev.payload})
                ev.ai_notice_seq = msg.seq
            if ev.ai_wake_state == "assigned":
                wake = db.get(AiOperation, ev.ai_wake_turn_id, populate_existing=True) if ev.ai_wake_turn_id else None
                if wake is not None and wake.status in ("succeeded", "waiting_input") and str(ev.id) in _context_ids(wake):
                    _close(ev, "consumed")
                elif wake is None or wake.status in (*asst.TERMINAL, "waiting_input"):
                    _close(ev, "suppressed" if wake and wake.status == "cancelled" else "delivery_failed", "wake_did_not_consume_context")
                continue
            active = list(db.scalars(select(AiOperation).where(AiOperation.conversation_id == cid,
                                       AiOperation.status.in_(("pending", "running", "waiting_input")), *orchestrator._turn_filter())))
            if any(turn.status == "running" for turn in active):
                continue
            root_id = _uuid(ev.payload["root_operation_id"])
            eligible = next((turn for turn in active if turn.status == "pending" and orchestrator.root_of(turn) == root_id), None)
            if eligible is None and active:
                continue
            if eligible is None:
                root = db.get(AiOperation, root_id)
                if root is None:
                    _close(ev, "suppressed")
                    continue
                # Legacy queued wakes keep their original client request identity.
                crid = uuid.uuid5(ev.id, "resume")
                eligible = orchestrator._turn_of(db, cid, crid)
                if eligible is None:
                    eligible = orchestrator._queue_turn(db, conv, root,
                        {"type": "sub_agent", "operation_id": str(ev.ai_operation_id), "event_id": str(ev.id),
                         "generation": ev.ai_generation, "outcome": ev.payload["outcome"], "status": ev.payload["status"]}, crid)
            ev.ai_wake_state, ev.ai_wake_turn_id = "assigned", eligible.id
            count += 1
        db.flush()
    return count


def context(db: Session, op: AiOperation) -> list[dict]:
    """Required immutable payloads assigned to this exact root/turn; never latest child state."""
    from . import orchestrator

    rows = db.scalars(select(OutboxEvent).where(OutboxEvent.kind == KIND, OutboxEvent.ai_wake_turn_id == op.id,
                                               OutboxEvent.ai_wake_state == "assigned")
                      .order_by(OutboxEvent.created_at, OutboxEvent.ai_event_seq, OutboxEvent.id)).all()
    return [copy.deepcopy(e.payload) for e in rows if e.payload.get("root_operation_id") == str(orchestrator.root_of(op))]
