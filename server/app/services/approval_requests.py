"""Approval requests: one inbox for engineering reviews and task acceptance
(design §7; contract §3).

- `engineering`: opened when a human_review node starts waiting (engine hook
  `on_node_waiting`), target = the reviewed subject's asset version, assignee
  NULL = any member whose role may decide the stage. Deciding it from the
  approval center and deciding the node on the run page both go through
  `decide_node`, which records the engineering approval (approvals.decide),
  completes the waiting node and closes the request.
- `task_acceptance`: opened by a submission; deciding it approves (task→done,
  after re-checking) or rejects (task→in_progress) the submission.
- `milestone_review`: opened by a stage review (services/stages.py); deciding
  it re-checks the stage guard and advances the current stage, or rejects.
- `engineering` without a run step: a process module version review
  (services/modules.py); the decision is recorded and the request closed.

Decisions are append-only; repeating the decision that closed a request
returns the existing result, a different one is a 409. Every open / decide /
reassign writes an outbox event in the same transaction.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, false, or_, select
from sqlalchemy.orm import Session

from .. import policy as exec_policy
from ..errors import AppError, conflict, not_found
from ..models import (
    AgentProfile,
    ApprovalDecision,
    ApprovalRequest,
    AuditLog,
    CollaborationNodeTask,
    CollaborationRun,
    NodeRun,
    Project,
    ProjectMember,
    Task,
    TaskSubmission,
    User,
    WorkflowRun,
)
from ..security import ACTION_ROLES, STAGE_ACTION, ProjectAccess, project_access
from . import approvals, feature_flags as features, outbox
from .audit import audit

log = logging.getLogger("partroute.approvals")

# Workflow port value type -> asset type (approvals.PORT_SUBJECT gives the subject type).
PORT_ASSET = {"model": "part_model", "drawing": "drawing", "spec": "intake", "plan": "process_plan", "quote": "quote"}
CLOSED_DECISION_STATES = ("approved", "rejected")


def _now() -> datetime:
    return datetime.now(UTC)


def _assets():
    from . import assets as mod

    return mod


def _node_params(run: WorkflowRun, node_id: str) -> dict:
    return next(((n.get("params") or {}) for n in run.graph_snapshot.get("nodes", []) if n.get("id") == node_id), {})


def _node_label(run: WorkflowRun, node_id: str) -> str:
    return next((n.get("label") or n["id"] for n in run.graph_snapshot.get("nodes", []) if n.get("id") == node_id), node_id)


def lock_request(db: Session, request_id: uuid.UUID) -> ApprovalRequest:
    r = db.scalar(select(ApprovalRequest).where(ApprovalRequest.id == request_id).with_for_update().execution_options(populate_existing=True))
    if r is None:
        raise not_found("approval request")
    return r


def _close(db: Session, req: ApprovalRequest, status: str) -> None:
    req.status = status
    req.closed_at = _now()
    req.version += 1


# --- direct part requests -------------------------------------------------------
SUBJECT_ASSET = {"model_revision": "part_model", "drawing": "drawing", "intake": "intake",
                 "process_plan": "process_plan", "quote": "quote"}


def subject_identity(version) -> tuple[str, uuid.UUID] | None:
    for subject, asset_type in SUBJECT_ASSET.items():
        value = getattr(version, _assets().KIND_FIELD[asset_type], None)
        if value is not None:
            return subject, value
    return None


def subject_path(subject: approvals.Subject) -> str:
    base = f"/parts/{subject.part.id}"
    suffix = {"model_revision": f"/model?rev={subject.id}", "drawing": f"/drawings?drawing={subject.id}",
              "intake": f"/intake?intake={subject.id}", "process_plan": f"/process?plan={subject.id}",
              "quote": f"/quote?quote={subject.id}"}[subject.type]
    return base + suffix


def part_subject(db: Session, user: User, part_id: uuid.UUID, subject_type: str, subject_id: uuid.UUID,
                 stage: str, *, write: bool = False):
    from ..models import Part
    from . import lifecycle

    part = db.get(Part, part_id)
    if part is None:
        raise not_found("part")
    acc = project_access(db, user, part.project_id)
    asset_type = SUBJECT_ASSET.get(subject_type)
    raw = db.get(_assets().KIND_MODEL[asset_type], subject_id) if asset_type else None
    if raw is None or raw.part_id != part_id:
        raise not_found("subject")
    if subject_type not in approvals.STAGE_SUBJECTS.get(stage, set()):
        raise AppError(400, "stage_subject_mismatch", "the subject does not belong to this stage")
    if write:
        acc.require(STAGE_ACTION[stage])
        lifecycle.lock_project(db, part.project_id)
        part = db.scalar(select(Part).where(Part.id == part_id).with_for_update().execution_options(populate_existing=True))
        _part_writable(acc.project, part)
    return approvals.load_subject(db, subject_type, subject_id), acc


def _part_writable(project: Project, part) -> None:
    if project.archived_at is not None or project.status == "archived":
        raise conflict("project_archived", "the project is archived")
    if part.archived_at is not None:
        raise conflict("part_archived", "the part is archived")
    if project.lifecycle not in ("active", "on_hold"):
        raise conflict("project_not_active", "engineering review needs an active or on-hold project")


def subject_out(db: Session, user: User, part_id: uuid.UUID, subject_type: str, subject_id: uuid.UUID, stage: str) -> dict:
    from .status import stale_reasons

    subject, acc = part_subject(db, user, part_id, subject_type, subject_id, stage)
    reasons = stale_reasons(db, subject)
    can_request = acc.can(STAGE_ACTION[stage]) and not reasons
    try:
        _part_writable(acc.project, subject.part)
    except AppError:
        can_request = False
    return {"subject_type": subject_type, "subject_id": str(subject_id), "stage": stage,
            "current_hash": subject.hash, "can_request": can_request, "stale_reasons": reasons,
            "source_path": subject_path(subject)}


def create_part_request(db: Session, user: User, part_id: uuid.UUID, *, subject_type: str, subject_id: uuid.UUID,
                        stage: str, expected_hash: str, assignee_id: uuid.UUID | None = None,
                        assignee_supplied: bool = False) -> tuple[ApprovalRequest, bool]:
    from .status import stale_reasons

    subject, acc = part_subject(db, user, part_id, subject_type, subject_id, stage, write=True)
    if subject.hash != expected_hash:
        raise conflict("subject_changed", "the subject changed; reload before requesting review")
    reasons = stale_reasons(db, subject)
    if reasons:
        raise conflict("subject_stale", "the subject is out of date", reasons=reasons)
    if assignee_id is not None:
        member = db.get(ProjectMember, {"project_id": acc.project.id, "user_id": assignee_id})
        if member is None:
            raise AppError(422, "not_a_member", "the assignee must be a project member")
        if member.role not in ACTION_ROLES[STAGE_ACTION[stage]]:
            raise AppError(422, "invalid_assignee", "the assignee cannot decide this stage")
    version = _assets().ensure_version(db, SUBJECT_ASSET[subject_type], subject_id)
    if version is None:
        raise conflict("subject_not_ready", "no reviewable asset version exists")
    existing = db.scalar(select(ApprovalRequest).where(
        ApprovalRequest.project_id == acc.project.id, ApprovalRequest.kind == "engineering",
        ApprovalRequest.asset_version_id == version.id, ApprovalRequest.stage == stage,
        ApprovalRequest.target_hash == expected_hash, ApprovalRequest.status == "pending",
        ApprovalRequest.run_id.is_(None), ApprovalRequest.node_run_id.is_(None)).with_for_update())
    if existing is not None:
        if assignee_supplied and assignee_id != existing.assignee_id:
            raise conflict("request_pending", "use the existing request's reassign action", request_id=str(existing.id))
        return existing, True
    req = ApprovalRequest(project_id=acc.project.id, kind="engineering", stage=stage, asset_version_id=version.id,
                          target_hash=subject.hash, assignee_id=assignee_id, status="pending", created_by=user.id,
                          title=f"{subject.part.number} · {subject_type} · {stage}"[:300])
    db.add(req)
    db.flush()
    audit(db, action="request.create", subject_type="approval_request", subject_id=req.id, user_id=user.id,
          project_id=req.project_id, data={"part_id": str(part_id), "subject_type": subject_type,
                                         "subject_id": str(subject_id), "stage": stage, "hash": expected_hash})
    outbox.request_opened(db, req, None)
    return req, False


def part_requests(db: Session, user: User, part_id: uuid.UUID, *, subject_type=None, subject_id=None,
                  stage=None, status="pending") -> list[ApprovalRequest]:
    from ..models import Asset, AssetVersion, Part

    part = db.get(Part, part_id)
    if part is None:
        raise not_found("part")
    project_access(db, user, part.project_id)
    if (subject_type is None) != (subject_id is None):
        raise AppError(422, "validation_error", "subject_type and subject_id must be supplied together")
    stmt = (select(ApprovalRequest).join(AssetVersion, AssetVersion.id == ApprovalRequest.asset_version_id)
            .join(Asset, Asset.id == AssetVersion.asset_id).where(Asset.part_id == part_id,
             ApprovalRequest.kind == "engineering", ApprovalRequest.run_id.is_(None), ApprovalRequest.node_run_id.is_(None)))
    if subject_type is not None:
        if subject_type not in SUBJECT_ASSET:
            raise AppError(422, "validation_error", "unknown subject type")
        stmt = stmt.where(getattr(AssetVersion, _assets().KIND_FIELD[SUBJECT_ASSET[subject_type]]) == subject_id)
    if stage is not None:
        stmt = stmt.where(ApprovalRequest.stage == stage)
    if status is not None:
        stmt = stmt.where(ApprovalRequest.status == status)
    return list(db.scalars(stmt.order_by(ApprovalRequest.created_at.desc())).all())


def _decide_part(db: Session, user: User, acc: ProjectAccess, req: ApprovalRequest, identity: tuple[str, uuid.UUID],
                 *, decision: str, comment: str, expected_hash: str) -> ApprovalRequest:
    from ..models import Part
    from . import lifecycle
    from .status import stale_reasons

    raw = db.get(_assets().KIND_MODEL[SUBJECT_ASSET[identity[0]]], identity[1])
    part = db.get(Part, raw.part_id) if raw is not None else None
    if part is None or part.project_id != req.project_id:
        raise conflict("request_stale", "the review subject is no longer available")
    lifecycle.lock_project(db, req.project_id)
    part = db.scalar(select(Part).where(Part.id == part.id).with_for_update().execution_options(populate_existing=True))
    req = lock_request(db, req.id)
    acc.require(STAGE_ACTION[req.stage or "design"])
    if req.assignee_id is not None and req.assignee_id != user.id:
        raise AppError(403, "not_assignee", "this review is assigned to someone else")
    if expected_hash != req.target_hash:
        raise conflict("subject_changed", "reload the request before deciding")
    if _closed_result(req, decision):
        return req
    _part_writable(acc.project, part)
    approval = approvals.decide(db, user, subject_type=identity[0], subject_id=identity[1], stage=req.stage or "design",
                                decision=decision, comment=comment, expected_hash=expected_hash, stale_check=stale_reasons)
    db.add(ApprovalDecision(request_id=req.id, decision=decision, comment=comment, actor_id=user.id,
                           role=acc.role, target_hash=approval.subject_hash))
    _close(db, req, decision)
    audit(db, action=f"request.{decision}", subject_type="approval_request", subject_id=req.id, user_id=user.id,
          project_id=req.project_id, data={"approval_id": str(approval.id), "part_id": str(part.id), "stage": req.stage})
    outbox.request_decided(db, req, user.id, decision)
    db.flush()
    return req


# --- engineering requests from runs ---------------------------------------------


def on_node_waiting(db: Session, run: WorkflowRun, nr: NodeRun) -> ApprovalRequest | None:
    """Engine hook: a node entered waiting_human. Opens an engineering request
    for human_review nodes (idempotent per node run)."""
    if nr.node_type != "human_review":
        return None
    if (run.execution_policy or "standard") == "turbo":  # W7: Turbo never opens an approval request (guard)
        return None
    existing = db.scalar(select(ApprovalRequest).where(ApprovalRequest.node_run_id == nr.id, ApprovalRequest.status == "pending"))
    if existing is not None:
        return existing
    subject = (nr.outputs or {}).get("subject")
    if not isinstance(subject, dict) or subject.get("type") not in PORT_ASSET:
        return None
    subject_type, subject_id = approvals.subject_from_port(subject)
    # ensure_version = version_for, registering rows that predate the catalogue (idempotent).
    version = _assets().ensure_version(db, PORT_ASSET[subject["type"]], subject_id)
    if version is None:
        log.warning("no asset version for %s %s; run %s node %s has no approval request", subject_type, subject_id, run.id, nr.node_id)
        return None
    subj = approvals.load_subject(db, subject_type, subject_id)
    params = _node_params(run, nr.node_id)
    stage = params.get("stage", "design")
    title = f"{_node_label(run, nr.node_id)} · {run.title}".strip(" ·") if run.title else _node_label(run, nr.node_id)
    req = ApprovalRequest(
        project_id=run.project_id,
        kind="engineering",
        stage=stage,
        asset_version_id=version.id,
        target_hash=subj.hash,
        assignee_id=None,
        status="pending",
        task_id=run.task_id,
        run_id=run.id,
        node_run_id=nr.id,
        title=title[:300],
        created_by=run.created_by,
    )
    db.add(req)
    db.flush()
    outbox.request_opened(db, req, None)
    return req


def close_for_run(db: Session, run_id: uuid.UUID, *, status: str = "withdrawn") -> int:
    """Close pending engineering requests of a run that will never be decided (run cancelled)."""
    rows = db.scalars(select(ApprovalRequest).where(ApprovalRequest.run_id == run_id, ApprovalRequest.status == "pending").with_for_update()).all()
    for r in rows:
        _close(db, r, status)
    return len(rows)


def _pending_for_node(db: Session, node_run_id: uuid.UUID) -> ApprovalRequest | None:
    return db.scalar(
        select(ApprovalRequest).where(ApprovalRequest.node_run_id == node_run_id, ApprovalRequest.status == "pending")
        .with_for_update().execution_options(populate_existing=True)
    )


def decide_node(
    db: Session,
    user: User,
    run: WorkflowRun,
    nr: NodeRun,
    *,
    decision: str,
    comment: str,
    expected_hash: str | None,
    request: ApprovalRequest | None = None,
) -> ApprovalRequest | None:
    """The single path for deciding a waiting human_review node (run page and
    approval center). Caller holds the run lock and commits."""
    from ..services.status import stale_reasons
    from ..workflow import engine

    if nr.status != "waiting_human":
        raise conflict("not_waiting", "this step is not waiting for a decision")
    # review.human (W0 §2.1): a person's engineering approval; a Turbo run never creates one (approver, time, decision).
    if exec_policy.run_check("review.human", run.execution_policy, dict).get("status") == "skipped_turbo":
        raise conflict("turbo_no_approval", "a Turbo run has no approvals; its outputs stay unverified")
    project = db.get(Project, run.project_id)
    assert project is not None
    if project.lifecycle in ("completed", "cancelled"):
        raise conflict("project_not_active", f"the project is {project.lifecycle}; no new decisions", lifecycle=project.lifecycle)
    req = request if request is not None else _pending_for_node(db, nr.id)
    if req is not None and req.assignee_id is not None and req.assignee_id != user.id:
        raise AppError(403, "not_assignee", "this review is assigned to someone else")
    stage = _node_params(run, nr.node_id).get("stage", "design")
    subject_type, subject_id = approvals.subject_from_port((nr.outputs or {}).get("subject"))
    project_access(db, user, run.project_id, STAGE_ACTION.get(stage, "design"))
    features.require_run_continuation(db, run.id)
    approval = approvals.decide(
        db,
        user,
        subject_type=subject_type,
        subject_id=subject_id,
        stage=stage,
        decision=decision,
        comment=comment,
        expected_hash=expected_hash,
        node_run_id=nr.id,
        stale_check=stale_reasons,
    )
    detail: dict[str, Any] = {"by": user.name, "decision": decision, "comment": comment}
    if req is not None:
        detail["request_id"] = str(req.id)
    if project.lifecycle == "on_hold":
        detail["project_on_hold"] = True
    nr.steps = (nr.steps or []) + [{"kind": "decision", "ok": decision == "approved", "at": _now().isoformat(), "detail": detail}]
    rejected_error = {"code": "rejected", "message": comment or "rejected by reviewer", "details": {"by": user.name}}
    engine.complete_waiting(db, run, nr.node_id, approved=decision == "approved", error=rejected_error)
    if req is not None:
        db.add(ApprovalDecision(request_id=req.id, decision=decision, comment=comment, actor_id=user.id, role=approval.role, target_hash=approval.subject_hash))
        _close(db, req, decision)
        audit(db, action=f"request.{decision}", subject_type="approval_request", subject_id=req.id, user_id=user.id, project_id=req.project_id,
              data={"kind": req.kind, "stage": stage, "node_run_id": str(nr.id), "on_hold": project.lifecycle == "on_hold"})
        outbox.request_decided(db, req, user.id, decision)
    db.flush()
    return req


# --- approval center decisions ----------------------------------------------------


def _closed_result(req: ApprovalRequest, decision: str) -> bool:
    """True = idempotent repeat of the closing decision; raises for anything else."""
    if req.status == "pending":
        return False
    if req.status in CLOSED_DECISION_STATES and req.status == decision:
        return True
    raise conflict("request_closed", f"this request is already {req.status}", status=req.status)


def _self_review_allowed() -> bool:
    from .submissions import allow_self_review

    return allow_self_review()


# --- collaboration acceptance (design §4 C05) ----------------------------------------------


def collab_context(db: Session, task_id: uuid.UUID | None) -> tuple[CollaborationNodeTask | None, CollaborationRun | None]:
    """The collaboration node task (and its run metadata) behind a task, if any."""
    if task_id is None:
        return None, None
    nt = db.scalar(select(CollaborationNodeTask).where(CollaborationNodeTask.task_id == task_id))
    return nt, (db.get(CollaborationRun, nt.run_id) if nt is not None else None)


def _agent_decides(nt: CollaborationNodeTask | None, collab: CollaborationRun | None) -> bool:
    return nt is not None and (nt.reviewer_kind == "agent" or (collab is not None and collab.execution_mode == "full_auto"))


def _collab_reviewer_problem(nt: CollaborationNodeTask, sub: TaskSubmission | None, user_id: uuid.UUID) -> tuple[str, str] | None:
    """Why this person may not make the final decision of a collaboration node, or None."""
    if nt.reviewer_kind != "human" or nt.reviewer_user_id != user_id:
        return "actor_not_allowed", "only the node's designated final reviewer can decide this acceptance"
    if nt.executor_kind == "human" and nt.executor_user_id == user_id:
        return "self_review_forbidden", "the executor of a task cannot accept it"
    if sub is not None and sub.submitted_by is not None and sub.submitted_by == user_id:
        return "self_review_forbidden", "you cannot accept your own submission"
    return None


def _require_collab_reviewer(nt: CollaborationNodeTask, sub: TaskSubmission, user: User) -> None:
    problem = _collab_reviewer_problem(nt, sub, user.id)
    if problem is not None:
        raise AppError(403, problem[0], problem[1], {"node_id": nt.node_id})


def validate_criterion_decisions(
    db: Session,
    nt: CollaborationNodeTask,
    sub: TaskSubmission,
    *,
    decision: str,
    items: list[dict[str, Any]] | None,
    comment: str,
) -> list[dict[str, Any]]:
    """Per-criterion final decisions of a collaboration acceptance (C05), against the
    submission's frozen criteria: every required criterion decided
    (`criteria_incomplete`), approval only when every required criterion passes
    (`criteria_not_passed`; unknown never passes), evidence limited to the
    submission items and the node's bound inputs (`evidence_unknown`), and a
    rejection needs a comment. Returns the normalized list stored on the decision."""
    if decision == "rejected" and not (comment or "").strip():
        raise AppError(422, "comment_required", "explain the rejection so the task can be reworked")
    if items is None:
        raise AppError(422, "criteria_incomplete", "decide every required acceptance criterion (criterion_decisions)")
    snap = sub.snapshot or {}
    crits = {str(c["id"]): c for c in snap.get("criteria") or []}
    known = {str(i["asset_version_id"]) for i in snap.get("items") or []} | {str(b.get("asset_version_id")) for b in nt.bound_inputs or []}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in items:
        it = raw.model_dump(mode="json") if hasattr(raw, "model_dump") else dict(raw)
        cid = str(it.get("criterion_id"))
        if cid not in crits:
            raise AppError(422, "unknown_criterion", "a decision names a criterion this submission does not have", {"criterion_id": cid})
        if cid in seen:
            raise AppError(422, "duplicate_criterion", "a criterion is decided twice", {"criterion_id": cid})
        seen.add(cid)
        result = it.get("result")
        if result not in ("pass", "fail", "unknown"):
            raise AppError(422, "unknown_result", "a criterion result is pass, fail or unknown", {"criterion_id": cid})
        reason = str(it.get("reason") or "").strip()
        if not reason:
            raise AppError(422, "reason_required", "every criterion decision needs a reason", {"criterion_id": cid})
        evidence = [str(e) for e in it.get("evidence_asset_version_ids") or []]
        bad = [e for e in evidence if e not in known]
        if bad:
            raise AppError(422, "evidence_unknown", "evidence must be a submitted version or a bound input of this node",
                           {"criterion_id": cid, "asset_version_ids": bad})
        row = {"criterion_id": cid, "result": result, "reason": reason[:4000], "evidence_asset_version_ids": evidence[:100]}
        if it.get("evidence"):  # P4: what a tool_agent review read for this criterion (agent path only; the HTTP body forbids it)
            from ..ai.evidence import sanitize_evidence

            row["evidence"] = sanitize_evidence(it["evidence"], known)
        out.append(row)
    missing = [cid for cid, c in crits.items() if c.get("required", True) and cid not in seen]
    if missing:
        raise AppError(422, "criteria_incomplete", "decide every required acceptance criterion", {"missing": missing})
    if decision == "approved":
        not_passed = [d for d in out if crits[d["criterion_id"]].get("required", True) and d["result"] != "pass"]
        if not_passed:
            raise AppError(422, "criteria_not_passed", "approval needs every required criterion to pass (unknown never passes)",
                           {"criteria": [{"criterion_id": d["criterion_id"], "result": d["result"]} for d in not_passed]})
    return out


def _collab_hooks(db: Session, *, run: WorkflowRun, nt: CollaborationNodeTask, sub: TaskSubmission, row: ApprovalDecision, actor, decision: str,
                  comment: str) -> None:
    from . import agent_runtime

    if decision == "approved":
        agent_runtime.on_task_accepted(db, run=run, nt=nt, submission=sub, decision_row=row, actor=actor)
    else:
        agent_runtime.on_task_rejected(db, run=run, nt=nt, submission=sub, decision_row=row, actor=actor, comment=comment)


def decide_as_agent(
    db: Session,
    *,
    agent_profile: AgentProfile,
    execution: Any,
    request_id: uuid.UUID,
    decision: str,
    comment: str,
    criterion_decisions: list[dict[str, Any]],
    lease_token: uuid.UUID | None = None,
) -> tuple[ApprovalRequest, ApprovalDecision]:
    """Final acceptance by the independent review agent of a full-auto node (the
    worker's path; never HTTP). Same checks as a human decision plus: the agent
    is the node's frozen reviewer, enabled, not the submitter, and (when
    `lease_token` is given) still holds the node's lease (A15). Lock order:
    task → submission → request → run → node task. Caller commits."""
    from . import agent_runtime, submissions
    from .actors import of_agent

    req = db.get(ApprovalRequest, request_id)
    if req is None:
        raise not_found("approval request")
    if req.kind != "task_acceptance" or req.submission_id is None:
        raise conflict("not_task_acceptance", "an agent only decides task acceptance requests")
    if decision not in CLOSED_DECISION_STATES:
        raise AppError(422, "unknown_decision", "decision is approved or rejected")
    sub = db.get(TaskSubmission, req.submission_id)
    if sub is None:
        raise not_found("submission")
    nt, collab = collab_context(db, sub.task_id)
    if nt is None or collab is None:
        raise conflict("not_collaboration", "this request does not belong to a collaboration node")
    if collab.execution_mode != "full_auto" or nt.reviewer_kind != "agent":
        raise conflict("mode_forbids_agent_decision", "in semi-auto mode the designated person makes the final decision; an agent only pre-reviews",
                       execution_mode=collab.execution_mode)
    if nt.reviewer_agent_id != agent_profile.id:
        raise AppError(403, "actor_not_allowed", "this agent is not the node's final reviewer", {"agent_profile_id": str(agent_profile.id)})
    if not agent_profile.enabled:
        raise AppError(422, "agent_disabled", f"agent {agent_profile.name} is disabled", {"agent_profile_id": str(agent_profile.id)})
    if execution is not None and (execution.node_task_id != nt.id or execution.role != "review"):
        raise AppError(403, "actor_not_allowed", "the execution is not this node's final review")
    features.require_run_continuation(db, nt.run_id)
    sub, task = submissions.lock_for_decision(db, sub)  # task, then submission
    req = lock_request(db, request_id)
    if _closed_result(req, decision):
        prev = db.scalar(select(ApprovalDecision).where(ApprovalDecision.request_id == req.id).order_by(ApprovalDecision.created_at.desc()).limit(1))
        assert prev is not None
        return req, prev
    if sub.snapshot_hash != req.target_hash:
        raise conflict("target_changed", "the submission changed after it was reviewed", current_hash=req.target_hash)
    if sub.submitted_by_kind == "agent" and sub.submitted_by_agent_id == agent_profile.id:
        raise AppError(403, "self_review_forbidden", "the submitting agent cannot accept its own submission")
    project = db.get(Project, req.project_id)
    assert project is not None
    if project.archived_at is not None:
        raise conflict("project_archived", "the project is archived")
    allowed = ("active",) if decision == "approved" else ("active", "on_hold")
    if project.lifecycle not in allowed:
        raise conflict("project_not_active", f"the project is {project.lifecycle}; task acceptance needs an active project", lifecycle=project.lifecycle)
    if sub.status != "pending":
        raise conflict("submission_closed", f"the submission is already {sub.status}", status=sub.status)
    crit = validate_criterion_decisions(db, nt, sub, decision=decision, items=criterion_decisions, comment=comment)
    run, nt = agent_runtime.lock_for_decision(db, nt, decision=decision, submission=sub, lease_token=lease_token)
    with features.continuation_scope(db, nt.run_id) if nt is not None else nullcontext():
        old_status = task.status
        if decision == "approved":
            submissions.accept(db, sub, task)
        else:
            submissions.reject(db, sub, task)
        row = ApprovalDecision(request_id=req.id, decision=decision, comment=comment, actor_id=None, actor_kind="agent", actor_agent_id=agent_profile.id,
                               role="agent", target_hash=req.target_hash, criterion_decisions=crit)
        db.add(row)
        db.flush()
        _close(db, req, decision)
        db.add(AuditLog(project_id=req.project_id, user_id=None, agent_id=agent_profile.id, action=f"request.{decision}", subject_type="approval_request",
                        subject_id=str(req.id), data={"kind": req.kind, "submission_id": str(sub.id), "task_id": str(task.id), "comment": comment,
                                                      "execution_id": str(execution.id) if execution is not None else None}))
        outbox.request_decided(db, req, None, decision)  # type: ignore[arg-type]
        _collab_hooks(db, run=run, nt=nt, sub=sub, row=row, actor=of_agent(agent_profile), decision=decision, comment=comment)
        if task.status != old_status:
            outbox.task_status(db, task, None, old_status, comment)
        db.flush()
        return req, row


def decide(db: Session, user: User, request_id: uuid.UUID, *, decision: str, comment: str, expected_hash: str,
           criterion_decisions: list[dict[str, Any]] | None = None) -> ApprovalRequest:
    """Decide a request from the approval center. Caller commits.

    Collaboration acceptance (C05): a full-auto node is decided only by its
    review agent (409 mode_forbids_human_decision); a semi-auto node needs
    `criterion_decisions` covering every required criterion."""
    from . import submissions
    from ..workflow import engine

    req = db.get(ApprovalRequest, request_id)
    if req is None:
        raise not_found("approval request")
    acc = project_access(db, user, req.project_id)
    if decision not in CLOSED_DECISION_STATES:
        raise AppError(422, "unknown_decision", "decision is approved or rejected")

    if req.kind == "milestone_review":
        from . import stages

        return stages.decide_review(db, user, acc, request_id, decision=decision, comment=comment, expected_hash=expected_hash)
    if req.kind == "engineering" and req.node_run_id is None and req.run_id is None:
        return decide_direct(db, user, acc, request_id, decision=decision, comment=comment, expected_hash=expected_hash)
    if req.kind == "engineering":
        if req.run_id is None or req.node_run_id is None:
            raise conflict("request_stale", "this request is not attached to a waiting step")
        run = engine.lock_run(db, req.run_id)  # same lock order as the run page: run, then request
        req = lock_request(db, request_id)
        if _closed_result(req, decision):
            return req
        if expected_hash != req.target_hash:
            raise conflict("target_changed", "the item changed after you opened it; reload and review again", current_hash=req.target_hash)
        nr = db.get(NodeRun, req.node_run_id)
        if nr is None or nr.status != "waiting_human":
            raise conflict("request_stale", "the run step is no longer waiting for this decision")
        decide_node(db, user, run, nr, decision=decision, comment=comment, expected_hash=req.target_hash, request=req)
        return req

    # task_acceptance
    sub = db.get(TaskSubmission, req.submission_id)
    if sub is None:
        raise not_found("submission")
    nt, collab = collab_context(db, sub.task_id)
    if _agent_decides(nt, collab):
        raise conflict("mode_forbids_human_decision", "a full-auto task is accepted only by its independent review agent",
                       execution_mode=collab.execution_mode if collab else None)
    if req.assignee_id != user.id:
        raise AppError(403, "not_assignee", "only the assigned reviewer can decide this request")
    if acc.role == "viewer":
        raise AppError(403, "forbidden", "a viewer cannot accept task results")
    if nt is not None:
        _require_collab_reviewer(nt, sub, user)
        features.require_run_continuation(db, nt.run_id)
    else:
        if sub.submitted_by == user.id and not _self_review_allowed():
            raise AppError(403, "self_review_forbidden", "you cannot accept your own submission")
        features.require_enabled("tasks")
    sub, task = submissions.lock_for_decision(db, sub)  # task, then submission
    req = lock_request(db, request_id)
    if _closed_result(req, decision):
        return req
    if expected_hash != req.target_hash or sub.snapshot_hash != req.target_hash:
        raise conflict("target_changed", "the submission changed after you opened it; reload and review again", current_hash=req.target_hash)
    if req.assignee_id != user.id:
        raise AppError(403, "not_assignee", "only the assigned reviewer can decide this request")
    if acc.role == "viewer":
        raise AppError(403, "forbidden", "a viewer cannot accept task results")
    if nt is not None:
        # Semi-auto final decision: only the node's frozen human reviewer, never its executor or the
        # submitter, whatever allow_self_review says (independence of the final acceptance, design §0.4).
        _require_collab_reviewer(nt, sub, user)
    elif sub.submitted_by == user.id and not _self_review_allowed():
        raise AppError(403, "self_review_forbidden", "you cannot accept your own submission")
    project = acc.project
    if project.archived_at is not None:
        raise conflict("project_archived", "the project is archived")
    allowed = ("active",) if decision == "approved" else ("active", "on_hold")
    if project.lifecycle not in allowed:
        raise conflict("project_not_active", f"the project is {project.lifecycle}; task acceptance needs an active project", lifecycle=project.lifecycle)
    if sub.status != "pending":
        raise conflict("submission_closed", f"the submission is already {sub.status}", status=sub.status)
    crit: list[dict[str, Any]] = []
    run: WorkflowRun | None = None
    if nt is not None:
        from . import agent_runtime

        crit = validate_criterion_decisions(db, nt, sub, decision=decision, items=criterion_decisions, comment=comment)
        run, nt = agent_runtime.lock_for_decision(db, nt, decision=decision, submission=sub)
    elif criterion_decisions:
        crit = [d.model_dump(mode="json") if hasattr(d, "model_dump") else dict(d) for d in criterion_decisions]
    with features.continuation_scope(db, nt.run_id) if nt is not None else nullcontext():
        old_status = task.status
        if decision == "approved":
            submissions.accept(db, sub, task)
        else:
            submissions.reject(db, sub, task)
        row = ApprovalDecision(request_id=req.id, decision=decision, comment=comment, actor_id=user.id, role=acc.role, target_hash=req.target_hash,
                               criterion_decisions=crit)
        db.add(row)
        db.flush()
        _close(db, req, decision)
        audit(db, action=f"request.{decision}", subject_type="approval_request", subject_id=req.id, user_id=user.id, project_id=req.project_id,
              data={"kind": req.kind, "submission_id": str(sub.id), "task_id": str(task.id), "comment": comment})
        outbox.request_decided(db, req, user.id, decision)
        if nt is not None and run is not None:
            from .actors import of_user

            _collab_hooks(db, run=run, nt=nt, sub=sub, row=row, actor=of_user(user), decision=decision, comment=comment)
        if task.status != old_status:
            outbox.task_status(db, task, user.id, old_status, comment)
        db.flush()
        return req


def decide_direct(db: Session, user: User, acc: ProjectAccess, request_id: uuid.UUID, *, decision: str, comment: str,
                  expected_hash: str) -> ApprovalRequest:
    """Engineering request on an asset version without a run step (process
    module review): record the decision and close the request. Caller commits."""
    from ..models import AssetVersion

    pre = db.get(ApprovalRequest, request_id)
    version = db.get(AssetVersion, pre.asset_version_id) if pre is not None and pre.asset_version_id else None
    identity = subject_identity(version) if version is not None else None
    if identity is not None:
        return _decide_part(db, user, acc, pre, identity, decision=decision, comment=comment, expected_hash=expected_hash)
    req = lock_request(db, request_id)
    if _closed_result(req, decision):
        return req
    v = db.get(AssetVersion, req.asset_version_id) if req.asset_version_id else None
    if v is None or v.process_module_id is None:
        raise conflict("request_stale", "this request is not attached to a waiting step")
    if expected_hash != req.target_hash or v.content_hash != req.target_hash:
        raise conflict("target_changed", "the item changed after you opened it; reload and review again", current_hash=req.target_hash)
    if req.assignee_id is not None and req.assignee_id != user.id:
        raise AppError(403, "not_assignee", "this review is assigned to someone else")
    stage = req.stage or "process"
    if acc.role not in ACTION_ROLES[STAGE_ACTION.get(stage, "process")]:
        raise AppError(403, "forbidden", f"a {acc.role} cannot decide a {stage} review", {"role": acc.role})
    features.require_enabled("process_modules")
    project = acc.project
    if project.archived_at is not None:
        raise conflict("project_archived", "the project is archived")
    if project.lifecycle in ("completed", "cancelled"):
        raise conflict("project_not_active", f"the project is {project.lifecycle}; no new decisions", lifecycle=project.lifecycle)
    db.add(ApprovalDecision(request_id=req.id, decision=decision, comment=comment, actor_id=user.id, role=acc.role, target_hash=req.target_hash))
    _close(db, req, decision)
    audit(db, action=f"request.{decision}", subject_type="approval_request", subject_id=req.id, user_id=user.id, project_id=req.project_id,
          data={"kind": req.kind, "stage": stage, "asset_version_id": str(req.asset_version_id), "comment": comment,
                "on_hold": project.lifecycle == "on_hold"})
    outbox.request_decided(db, req, user.id, decision)
    db.flush()
    return req


def reassign(db: Session, user: User, request_id: uuid.UUID, assignee_id: uuid.UUID) -> ApprovalRequest:
    req = db.get(ApprovalRequest, request_id)
    if req is None:
        raise not_found("approval request")
    project_access(db, user, req.project_id, "manage")
    feature = request_feature(db, req)
    if feature is not None:
        features.require_enabled(feature)
    if req.kind == "milestone_review" and req.milestone_review_id:
        # Same lock order as stages.decide_review / withdraw_review: project, milestone, review, request.
        from ..models import MilestoneReview
        from . import stages
        from .lifecycle import lock_project

        review = db.get(MilestoneReview, req.milestone_review_id)
        if review is not None:
            lock_project(db, req.project_id)
            stages.lock_milestone(db, review.milestone_id)
            stages._lock_review(db, review.id)
    req = lock_request(db, request_id)
    if req.status != "pending":
        raise conflict("request_closed", f"this request is already {req.status}", status=req.status)
    member = db.get(ProjectMember, {"project_id": req.project_id, "user_id": assignee_id})
    if member is None:
        raise AppError(422, "not_a_member", "the assignee must be a project member")
    if req.kind == "task_acceptance":
        sub = db.get(TaskSubmission, req.submission_id) if req.submission_id else None
        nt, _ = collab_context(db, sub.task_id if sub is not None else req.task_id)
        if nt is not None:
            # The frozen reviewer (agent in full-auto, the designated person in semi-auto) is the only final reviewer.
            raise conflict("use_collaboration_actions", "a collaboration task is accepted only by its node's frozen reviewer; it cannot be reassigned",
                           run_id=str(nt.run_id), node_id=nt.node_id)
        if member.role == "viewer":
            raise AppError(422, "read_only_member", "a viewer cannot accept task results")
        if sub is not None and sub.submitted_by == assignee_id and not _self_review_allowed():
            raise AppError(422, "self_review_forbidden", "the submitter cannot accept their own submission")
    elif req.kind == "milestone_review":
        from ..models import MilestoneReview

        if member.role == "viewer":
            raise AppError(422, "read_only_member", "a viewer cannot review a stage")
        review = db.get(MilestoneReview, req.milestone_review_id) if req.milestone_review_id else None
        if review is not None and review.submitted_by == assignee_id and not _self_review_allowed():
            raise AppError(422, "self_review_forbidden", "the submitter cannot review their own stage review")
    elif req.stage and member.role not in ACTION_ROLES[STAGE_ACTION[req.stage]]:
        raise AppError(422, "assignee_cannot_decide", f"a {member.role} cannot decide a {req.stage} review")
    old = req.assignee_id
    req.assignee_id = assignee_id
    req.version += 1
    if req.kind == "milestone_review" and req.milestone_review_id:
        from ..models import MilestoneReview

        review = db.get(MilestoneReview, req.milestone_review_id)
        if review is not None:
            review.reviewer_id = assignee_id
    audit(db, action="request.reassign", subject_type="approval_request", subject_id=req.id, user_id=user.id, project_id=req.project_id,
          data={"from": str(old) if old else None, "to": str(assignee_id)})
    outbox.request_reassigned(db, req, user.id, old)
    db.flush()
    return req


# --- queries and views --------------------------------------------------------------


def list_requests(
    db: Session,
    user: User,
    *,
    scope: str = "mine",
    project_id: uuid.UUID | None = None,
    status: str | None = "pending",
    kind: str | None = None,
    task_id: uuid.UUID | None = None,
    limit: int = 100,
) -> list[ApprovalRequest]:
    stmt = (
        select(ApprovalRequest)
        .join(ProjectMember, and_(ProjectMember.project_id == ApprovalRequest.project_id, ProjectMember.user_id == user.id))
        .order_by(ApprovalRequest.created_at.desc())
        .limit(min(max(limit, 1), 500))
    )
    if project_id:
        project_access(db, user, project_id)
        stmt = stmt.where(ApprovalRequest.project_id == project_id)
    if status and status != "all":
        stmt = stmt.where(ApprovalRequest.status.in_([s.strip() for s in status.split(",") if s.strip()]))
    if kind:
        stmt = stmt.where(ApprovalRequest.kind == kind)
    if task_id:
        stmt = stmt.where(ApprovalRequest.task_id == task_id)
    if scope == "mine":
        stage_conds = [and_(ApprovalRequest.stage == s, ProjectMember.role.in_(sorted(ACTION_ROLES[a]))) for s, a in STAGE_ACTION.items()]
        stmt = stmt.where(or_(
            ApprovalRequest.assignee_id == user.id,
            and_(ApprovalRequest.kind == "engineering", ApprovalRequest.assignee_id.is_(None), or_(*stage_conds) if stage_conds else false()),
        ))
    elif scope == "created":
        stmt = stmt.where(ApprovalRequest.created_by == user.id)
    elif scope != "all":
        raise AppError(422, "unknown_scope", "scope is mine, created or all")
    return list(db.scalars(stmt).all())


def request_feature(db: Session, req: ApprovalRequest) -> features.FeatureName | None:
    if req.kind == "task_acceptance":
        return "tasks"
    if req.kind == "milestone_review":
        return "stages"
    if req.asset_version_id is not None:
        from ..models import AssetVersion

        version = db.get(AssetVersion, req.asset_version_id)
        if version is not None and version.process_module_id is not None:
            return "process_modules"
    return None


def feature_allows_decision(db: Session, req: ApprovalRequest) -> bool:
    if req.run_id is not None:
        try:
            features.require_run_continuation(db, req.run_id)
        except AppError:
            return False
    feature = request_feature(db, req)
    if feature == "tasks":
        sub = db.get(TaskSubmission, req.submission_id) if req.submission_id else None
        task_id = sub.task_id if sub is not None else req.task_id
        nt, _ = collab_context(db, task_id)
        if nt is not None:
            return all(features.enabled(name) or features.can_continue(db, name, run_id=nt.run_id, task_id=task_id)
                       for name in ("tasks", "collaboration"))
    return feature is None or features.enabled(feature)


def can_decide(db: Session, user: User, req: ApprovalRequest, role: str | None = None, project: Project | None = None) -> bool:
    if not feature_allows_decision(db, req):
        return False
    if req.status != "pending":
        return False
    project = project or db.get(Project, req.project_id)
    if project is None or project.archived_at is not None or project.lifecycle in ("completed", "cancelled"):
        return False
    if role is None:
        m = db.get(ProjectMember, {"project_id": req.project_id, "user_id": user.id})
        role = m.role if m else None
    if role is None:
        return False
    if req.kind == "task_acceptance":
        if req.assignee_id != user.id or project.lifecycle != "active" or role == "viewer":
            return False
        sub = db.get(TaskSubmission, req.submission_id) if req.submission_id else None
        nt, collab = collab_context(db, sub.task_id if sub is not None else req.task_id)
        if nt is not None:
            return not _agent_decides(nt, collab) and _collab_reviewer_problem(nt, sub, user.id) is None
        return sub is None or sub.submitted_by != user.id or _self_review_allowed()
    if req.kind == "milestone_review":
        from ..models import MilestoneReview

        if req.assignee_id != user.id or project.lifecycle != "active" or role == "viewer":
            return False
        review = db.get(MilestoneReview, req.milestone_review_id) if req.milestone_review_id else None
        return review is None or review.submitted_by != user.id or _self_review_allowed()
    if req.kind == "engineering" and req.run_id is None:
        from ..models import AssetVersion, Part
        version = db.get(AssetVersion, req.asset_version_id) if req.asset_version_id else None
        identity = subject_identity(version) if version is not None else None
        if identity is not None:
            row = db.get(_assets().KIND_MODEL[SUBJECT_ASSET[identity[0]]], identity[1])
            part = db.get(Part, row.part_id) if row else None
            if part is None or part.archived_at is not None or project.lifecycle not in ("active", "on_hold"):
                return False
    if req.assignee_id is not None:
        return req.assignee_id == user.id and role in ACTION_ROLES[STAGE_ACTION.get(req.stage or "design", "design")]
    return role in ACTION_ROLES[STAGE_ACTION.get(req.stage or "design", "design")]


def can_reassign(db: Session, user: User, req: ApprovalRequest) -> bool:
    if req.status != "pending":
        return False
    member = db.get(ProjectMember, {"project_id": req.project_id, "user_id": user.id})
    if member is None or member.role not in ACTION_ROLES["manage"]:
        return False
    feature = request_feature(db, req)
    if feature is not None and not features.enabled(feature):
        return False
    if req.kind == "task_acceptance":
        sub = db.get(TaskSubmission, req.submission_id) if req.submission_id else None
        nt, _ = collab_context(db, sub.task_id if sub is not None else req.task_id)
        if nt is not None:  # A collaboration node keeps its frozen final reviewer.
            return False
    return True


def request_out(db: Session, user: User, req: ApprovalRequest) -> dict:
    task = db.get(Task, req.task_id) if req.task_id else None
    nr = db.get(NodeRun, req.node_run_id) if req.node_run_id else None
    assignee = db.get(User, req.assignee_id) if req.assignee_id else None
    creator = db.get(User, req.created_by)
    project = db.get(Project, req.project_id)
    return {
        "id": str(req.id),
        "project_id": str(req.project_id),
        "project": {"id": str(project.id), "code": project.code, "name": project.name} if project else None,
        "kind": req.kind,
        "stage": req.stage,
        "status": req.status,
        "title": req.title,
        "assignee_id": str(req.assignee_id) if req.assignee_id else None,
        "assignee": {"id": str(assignee.id), "name": assignee.name} if assignee else None,
        "task": {"id": str(task.id), "number": task.number, "key": f"T-{task.number}", "title": task.title, "status": task.status} if task else None,
        "run_id": str(req.run_id) if req.run_id else None,
        "node_run_id": str(req.node_run_id) if req.node_run_id else None,
        "node_id": nr.node_id if nr else None,
        "node_status": nr.status if nr else None,
        "asset_version_id": str(req.asset_version_id) if req.asset_version_id else None,
        "submission_id": str(req.submission_id) if req.submission_id else None,
        "milestone_review_id": str(req.milestone_review_id) if req.milestone_review_id else None,
        "target_hash": req.target_hash,
        "policy_version": req.policy_version,
        "version": req.version,
        "created_by": str(req.created_by),
        "created_by_name": creator.name if creator else None,
        "created_at": req.created_at.isoformat() if req.created_at else None,
        "due_at": req.due_at.isoformat() if req.due_at else None,
        "closed_at": req.closed_at.isoformat() if req.closed_at else None,
        "can_decide": can_decide(db, user, req),
        "can_reassign": can_reassign(db, user, req),
        **_collab_fields(db, req),
    }


def _collab_fields(db: Session, req: ApprovalRequest) -> dict[str, Any]:
    """request_out additions (design §4): node_task {run_id, node_id, execution_mode, phase}|null and decision_mode human|agent."""
    nt, collab = collab_context(db, req.task_id) if req.kind == "task_acceptance" else (None, None)
    if nt is None:
        return {"node_task": None, "decision_mode": "human"}
    return {"node_task": {"run_id": str(nt.run_id), "node_id": nt.node_id, "execution_mode": collab.execution_mode if collab else None,
                          "phase": nt.phase},
            "decision_mode": "agent" if _agent_decides(nt, collab) else "human"}


def decision_out(d: ApprovalDecision, db: Session) -> dict:
    from .actors import out_legacy

    who = out_legacy(db, d, "actor_id")
    name = ((who or {}).get("user") or {}).get("name") or ((who or {}).get("agent") or {}).get("name")
    return {"id": str(d.id), "decision": d.decision, "comment": d.comment, "actor_id": str(d.actor_id) if d.actor_id else None, "actor_name": name,
            "actor": who, "criterion_decisions": d.criterion_decisions or [],
            "role": d.role, "target_hash": d.target_hash, "created_at": d.created_at.isoformat() if d.created_at else None}


def request_detail(db: Session, user: User, req: ApprovalRequest) -> dict:
    from . import submissions

    data = request_out(db, user, req)
    data["decisions"] = [decision_out(d, db) for d in db.scalars(
        select(ApprovalDecision).where(ApprovalDecision.request_id == req.id).order_by(ApprovalDecision.created_at)).all()]
    target: dict[str, Any] = {}
    if req.kind == "engineering" and req.asset_version_id:
        from ..models import AssetVersion

        v = db.get(AssetVersion, req.asset_version_id)
        try:
            target["asset_version"] = _assets().version_out(db, v) if v else None
        except Exception:  # noqa: BLE001
            target["asset_version"] = {"id": str(req.asset_version_id)}
        nr = db.get(NodeRun, req.node_run_id) if req.node_run_id else None
        run = db.get(WorkflowRun, req.run_id) if req.run_id else None
        if nr is not None and run is not None:
            subject = (nr.outputs or {}).get("subject")
            try:
                st, sid = approvals.subject_from_port(subject)
                cur = approvals.load_subject(db, st, sid)
                target["subject"] = {"type": st, "id": str(sid), "current_hash": cur.hash, "changed": cur.hash != req.target_hash,
                                     "approved_now": approvals.is_approved(db, st, sid, req.stage or "design")}
            except AppError as exc:
                target["subject"] = {"error": exc.code}
            target["node"] = {"node_id": nr.node_id, "status": nr.status, "instructions": _node_params(run, nr.node_id).get("instructions", ""),
                              "run_title": run.title, "run_status": run.status}
        if nr is None and run is None and v is not None and subject_identity(v) is not None:
            st, sid = subject_identity(v)
            try:
                cur = approvals.load_subject(db, st, sid)
                target["part"] = {"id": str(cur.part.id), "number": cur.part.number, "name": cur.part.name}
                target["subject"] = {"type": st, "id": str(sid), "current_hash": cur.hash,
                                     "changed": cur.hash != req.target_hash,
                                     "approved_now": approvals.is_approved(db, st, sid, req.stage or "design")}
                target["source_path"] = subject_path(cur)
            except AppError as exc:
                target["subject"] = {"type": st, "id": str(sid), "error": exc.code}
        if nr is None and run is None and v is not None and v.process_module_id is not None:
            from .modules import module_out
            from ..models import ProcessModule

            row = db.get(ProcessModule, v.process_module_id)
            target["module"] = module_out(row) if row else None
            target["subject"] = {"type": "asset_version", "id": str(v.id), "current_hash": v.content_hash,
                                 "changed": v.content_hash != req.target_hash,
                                 "approved_now": _assets().module_engineering(db, v)["approved"]}
    elif req.kind == "milestone_review" and req.milestone_review_id:
        from . import stages

        target.update(stages.review_target(db, user, req))
    elif req.kind == "task_acceptance" and req.submission_id:
        sub = db.get(TaskSubmission, req.submission_id)
        if sub is not None:
            target["submission"] = submissions.submission_out(db, sub)
            if req.status == "pending":
                try:
                    target["current_checks"] = submissions.recheck(db, sub)
                except Exception:  # noqa: BLE001 - informative only
                    log.exception("recheck failed for submission %s", sub.id)
                    target["current_checks"] = None
    data["target"] = target
    return data
