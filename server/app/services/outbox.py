"""Outbox events and in-app notifications (design §10.3; contract §3.6).

`emit` inserts a durable event inside the business transaction (ON CONFLICT
DO NOTHING on `dedupe_key`, so a replayed write never doubles an event).
`dispatch_pending` turns unprocessed events into per-user notifications in a
separate transaction: rows are claimed with FOR UPDATE SKIP LOCKED, inserts
are idempotent per (event, user), and a failure only bumps `attempts` /
`last_error` — it never touches the business data the event reports.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..models import ApprovalRequest, AssetVersion, MilestoneReview, Notification, OutboxEvent, Project, ProjectMember, Task, User
from ..security import ACTION_ROLES, STAGE_ACTION
from . import feature_flags as features

log = logging.getLogger("partroute.outbox")

EVENT_KINDS = ("request.opened", "request.decided", "request.reassigned", "task.status", "review.decided", "impact.opened",
               "collaboration.completed", "knowledge.index")
# Events that run a service (knowledge archive / index) instead of producing notifications (design §3.7, §4 C09).
SERVICE_EVENTS = frozenset({"collaboration.completed", "knowledge.index"})
MAX_ATTEMPTS = 5


def _now() -> datetime:
    return datetime.now(UTC)


def _s(value: Any) -> str | None:
    return str(value) if value is not None else None


def emit(db: Session, *, kind: str, project_id: uuid.UUID | None, payload: dict[str, Any], dedupe_key: str) -> None:
    """Record one event in the caller's transaction (idempotent per dedupe_key)."""
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown outbox event {kind}")
    if kind in SERVICE_EVENTS and not features.enabled("collaboration"):
        return
    clean = {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in payload.items()}
    db.execute(
        pg_insert(OutboxEvent)
        .values(id=uuid.uuid4(), project_id=project_id, kind=kind, payload=clean, dedupe_key=dedupe_key[:200])
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
    )


# --- helpers the services call --------------------------------------------------


def request_payload(req: ApprovalRequest, actor_id: uuid.UUID | None, **extra: Any) -> dict[str, Any]:
    return {"request_id": req.id, "request_kind": req.kind, "stage": req.stage, "title": req.title, "assignee_id": req.assignee_id,
            "task_id": req.task_id, "milestone_review_id": req.milestone_review_id, "asset_version_id": req.asset_version_id,
            "created_by": req.created_by, "actor_id": actor_id, **extra}


def request_opened(db: Session, req: ApprovalRequest, actor_id: uuid.UUID | None) -> None:
    if req.id is None:
        db.flush()
    emit(db, kind="request.opened", project_id=req.project_id, payload=request_payload(req, actor_id), dedupe_key=f"request.opened:{req.id}")


def request_decided(db: Session, req: ApprovalRequest, actor_id: uuid.UUID, decision: str) -> None:
    emit(db, kind="request.decided", project_id=req.project_id, payload=request_payload(req, actor_id, decision=decision),
         dedupe_key=f"request.decided:{req.id}:{decision}")


def request_reassigned(db: Session, req: ApprovalRequest, actor_id: uuid.UUID, old_assignee: uuid.UUID | None) -> None:
    emit(db, kind="request.reassigned", project_id=req.project_id, payload=request_payload(req, actor_id, previous_assignee_id=old_assignee),
         dedupe_key=f"request.reassigned:{req.id}:{req.version}")


def task_status(db: Session, task: Task, actor_id: uuid.UUID | None, old: str, reason: str = "") -> None:
    emit(db, kind="task.status", project_id=task.project_id,
         payload={"task_id": task.id, "key": f"T-{task.number}", "title": task.title, "from": old, "to": task.status, "reason": reason,
                  "owner_id": task.owner_id, "reviewer_id": task.reviewer_id, "actor_id": actor_id},
         dedupe_key=f"task.status:{task.id}:{task.version}")


def review_decided(db: Session, review: MilestoneReview, actor_id: uuid.UUID, decision: str, milestone_name: str) -> None:
    emit(db, kind="review.decided", project_id=review.project_id,
         payload={"review_id": review.id, "milestone_id": review.milestone_id, "milestone_name": milestone_name, "decision": decision,
                  "submitted_by": review.submitted_by, "actor_id": actor_id},
         dedupe_key=f"review.decided:{review.id}:{decision}")


def impact_opened(db: Session, notice, actor_id: uuid.UUID | None) -> None:
    if notice.id is None:
        db.flush()
    emit(db, kind="impact.opened", project_id=notice.project_id,
         payload={"notice_id": notice.id, "reason": notice.reason, "severity": notice.severity, "task_id": notice.task_id,
                  "milestone_id": notice.milestone_id, "milestone_review_id": notice.milestone_review_id, "actor_id": actor_id},
         dedupe_key=f"impact.opened:{notice.id}")


# --- dispatch -------------------------------------------------------------------------


def _members(db: Session, project_id: uuid.UUID | None) -> dict[uuid.UUID, str]:
    if project_id is None:
        return {}
    return dict(db.execute(select(ProjectMember.user_id, ProjectMember.role).where(ProjectMember.project_id == project_id)).all())


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


def _render(db: Session, ev: OutboxEvent) -> tuple[list[uuid.UUID], str, str, str | None]:
    """(recipients, title, body, link) for one event. Recipients are current
    project members, never the user who caused the event."""
    p = ev.payload or {}
    members = _members(db, ev.project_id)
    project = db.get(Project, ev.project_id) if ev.project_id else None
    owners = [uid for uid, role in members.items() if role == "owner"]
    if project is not None and project.owner_id:
        owners.append(project.owner_id)
    want: list[uuid.UUID | None] = []
    actor = _uuid(p.get("actor_id"))
    actor_user = db.get(User, actor) if actor else None
    actor_name = actor_user.name if actor_user else ""
    prefix = f"[{project.code}] " if project else ""
    title, body, link = ev.kind, "", None
    kind = ev.kind
    if kind.startswith("request."):
        rid = p.get("request_id")
        link = f"/approval-requests/{rid}" if rid else None
        label = p.get("title") or p.get("request_kind") or "request"
        task = db.get(Task, _uuid(p.get("task_id"))) if p.get("task_id") else None
        if kind == "request.opened":
            assignee = _uuid(p.get("assignee_id"))
            if assignee:
                want.append(assignee)
            elif p.get("stage") in STAGE_ACTION:
                roles = ACTION_ROLES[STAGE_ACTION[p["stage"]]]
                want += [uid for uid, role in members.items() if role in roles]
            title = f"{prefix}Review requested: {label}"
            body = f"{actor_name} asked for a decision" if actor_name else "A decision is waiting"
        elif kind == "request.decided":
            want.append(_uuid(p.get("created_by")))
            if task is not None:
                want += [task.owner_id]
            if p.get("request_kind") == "milestone_review":
                want += owners
            title = f"{prefix}{label}: {p.get('decision', 'decided')}"
            body = f"decided by {actor_name}" if actor_name else ""
        else:  # request.reassigned
            want += [_uuid(p.get("assignee_id")), _uuid(p.get("previous_assignee_id"))]
            title = f"{prefix}Review reassigned: {label}"
            body = f"reassigned by {actor_name}" if actor_name else ""
    elif kind == "task.status":
        want += [_uuid(p.get("owner_id")), _uuid(p.get("reviewer_id"))]
        link = f"/tasks/{p.get('task_id')}"
        title = f"{prefix}{p.get('key', 'Task')} {p.get('title', '')}: {p.get('from')} → {p.get('to')}"
        body = p.get("reason") or (f"by {actor_name}" if actor_name else "")
    elif kind == "review.decided":
        want.append(_uuid(p.get("submitted_by")))
        want += owners
        link = f"/projects/{ev.project_id}/overview" if ev.project_id else None
        title = f"{prefix}Stage review {p.get('milestone_name', '')}: {p.get('decision')}"
        body = f"decided by {actor_name}" if actor_name else ""
    elif kind == "impact.opened":
        task = db.get(Task, _uuid(p.get("task_id"))) if p.get("task_id") else None
        if task is not None:
            want += [task.owner_id, task.reviewer_id]
            link = f"/tasks/{task.id}"
        else:
            link = f"/projects/{ev.project_id}/overview" if ev.project_id else None
        want += owners
        title = f"{prefix}Impact: {p.get('reason', '')}"
        body = f"T-{task.number} {task.title}" if task is not None else ""
    seen: list[uuid.UUID] = []
    for uid in want:
        if uid is not None and uid != actor and uid in members and uid not in seen:
            seen.append(uid)
    return seen, title[:300], body, link


def _dispatch_service(db: Session, kind: str, payload: dict[str, Any]) -> None:
    """Knowledge archive / index (backend E). Runs inside the event's savepoint: a failure
    rolls back only this attempt and is recorded by `_service_failed`."""
    from . import knowledge
    from .actors import ActorRef

    if kind == "collaboration.completed":
        run_id = _uuid(payload.get("run_id"))
        if run_id is None:
            raise ValueError("collaboration.completed without run_id")
        actor_user = _uuid(payload.get("actor_user_id"))
        if actor_user is None:
            from ..models import WorkflowRun

            run = db.get(WorkflowRun, run_id)
            actor_user = run.created_by if run else None
        if actor_user is None:
            raise ValueError("collaboration.completed without an actor")
        knowledge.archive(db, run_id, actor=ActorRef(kind="human", user_id=actor_user))
    elif kind == "knowledge.index":
        collection_id = _uuid(payload.get("collection_id"))
        if collection_id is None:
            raise ValueError("knowledge.index without collection_id")
        knowledge.run_index(db, collection_id)
    else:
        raise ValueError(f"unknown service event {kind}")


def _service_failed(db: Session, kind: str, payload: dict[str, Any], error: str) -> None:
    """Record a failed archive / index on the collaboration run / collection (never undoes anything)."""
    from . import knowledge

    if kind == "collaboration.completed":
        run_id = _uuid(payload.get("run_id"))
        if run_id is not None:
            knowledge.record_archive_failure(db, run_id, error)
    elif kind == "knowledge.index":
        collection_id = _uuid(payload.get("collection_id"))
        if collection_id is not None:
            knowledge.record_index_failure(db, collection_id, error)


def _frozen_event_feature(db: Session, ev: OutboxEvent) -> features.FeatureName | None:
    """Keep frozen links out of new notifications; live cohort decisions still notify."""
    payload = ev.payload or {}
    feature: features.FeatureName | None = None
    if ev.kind in SERVICE_EVENTS:
        feature = "collaboration"
    elif ev.kind == "task.status" or (ev.kind.startswith("request.") and payload.get("request_kind") == "task_acceptance"):
        task_id = _uuid(payload.get("task_id"))
        if not features.enabled("tasks") and task_id is not None and features.can_continue(db, "tasks", task_id=task_id):
            return None
        feature = "tasks"
    elif ev.kind == "review.decided" or (ev.kind.startswith("request.") and payload.get("request_kind") == "milestone_review"):
        feature = "stages"
    elif ev.kind == "impact.opened":
        feature = "tasks" if payload.get("task_id") else "stages"
    elif ev.kind.startswith("request.") and payload.get("asset_version_id"):
        version_id = _uuid(payload["asset_version_id"])
        version = db.get(AssetVersion, version_id) if version_id is not None else None
        if version is not None and version.process_module_id is not None:
            feature = "process_modules"
    return feature if feature is not None and not features.enabled(feature) else None


def dispatch_pending(db: Session, limit: int = 50) -> int:
    """Turn unprocessed events into notifications (or run the knowledge archive / index
    for service events). Returns the number of events processed. Caller commits."""
    active = [key for key, value in features.effective_features().items() if value]
    if active:
        db.execute(update(OutboxEvent).where(OutboxEvent.kind.in_(EVENT_KINDS), OutboxEvent.processed_at.is_(None), OutboxEvent.deferred_feature.in_(active))
                   .values(deferred_feature=None, deferred_at=None))
    if not features.enabled("collaboration"):
        db.execute(update(OutboxEvent).where(OutboxEvent.processed_at.is_(None), OutboxEvent.kind.in_(SERVICE_EVENTS),
                                            OutboxEvent.deferred_feature.is_(None))
                   .values(deferred_feature="collaboration", deferred_at=_now()))
    events = db.scalars(
        select(OutboxEvent)
        .where(OutboxEvent.kind.in_(EVENT_KINDS), OutboxEvent.processed_at.is_(None), OutboxEvent.attempts < MAX_ATTEMPTS, OutboxEvent.deferred_feature.is_(None))
        .order_by(OutboxEvent.created_at, OutboxEvent.id)
        .limit(max(1, limit))
        .with_for_update(skip_locked=True)
    ).all()
    done = 0
    for ev in events:
        if ev.kind not in EVENT_KINDS:  # Private AI completion has a dedicated consumer.
            continue
        frozen = _frozen_event_feature(db, ev)
        if frozen is not None:
            ev.deferred_feature, ev.deferred_at = frozen, _now()
            continue
        ev_id, kind, payload = ev.id, ev.kind, dict(ev.payload or {})
        try:
            with db.begin_nested():
                if kind in SERVICE_EVENTS:
                    _dispatch_service(db, kind, payload)
                else:
                    recipients, title, body, link = _render(db, ev)
                    for uid in recipients:
                        db.execute(
                            pg_insert(Notification)
                            .values(id=uuid.uuid4(), user_id=uid, event_id=ev.id, project_id=ev.project_id, kind=ev.kind,
                                    title=title, body=body, link=link)
                            .on_conflict_do_nothing(constraint="uq_notifications_event_user")
                        )
                ev.processed_at = _now()
            done += 1
        except Exception as exc:  # noqa: BLE001 - a bad event must not block the others
            log.exception("outbox event %s could not be dispatched", ev_id)
            db.execute(update(OutboxEvent).where(OutboxEvent.id == ev_id)
                       .values(attempts=OutboxEvent.attempts + 1, last_error=str(exc)[:2000]))
            if kind in SERVICE_EVENTS:
                try:
                    with db.begin_nested():
                        _service_failed(db, kind, payload, str(exc))
                except Exception:  # noqa: BLE001
                    log.exception("could not record the failure of %s %s", kind, ev_id)
    db.flush()
    return done


# --- views ------------------------------------------------------------------------------


def notification_out(n: Notification, event: OutboxEvent | None = None, project: Project | None = None) -> dict[str, Any]:
    return {
        "id": str(n.id),
        "kind": n.kind,
        "title": n.title,
        "body": n.body,
        "link": n.link,
        "project_id": _s(n.project_id),
        "project": {"id": str(project.id), "code": project.code, "name": project.name} if project else None,
        "payload": (event.payload if event is not None else {}) or {},
        "created_at": n.created_at.isoformat() if n.created_at else None,
        "read_at": n.read_at.isoformat() if n.read_at else None,
    }


def unread_count(db: Session, user_id: uuid.UUID) -> int:
    return int(db.scalar(select(func.count()).select_from(Notification).where(Notification.user_id == user_id, Notification.read_at.is_(None))) or 0)
