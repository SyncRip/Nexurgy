"""Human confirmation: who may approve what, bound to an exact subject version.

An approval stores the subject's hash at decision time. It only counts while
the subject still has that hash and is not stale (see services/status.py).
Copies never inherit approvals (parts.copy does not copy this table).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import AppError, bad_request, conflict, not_found
from ..models import Approval, Drawing, Intake, ModelRevision, Part, ProcessPlan, Quote, User
from ..security import STAGE_ACTION, project_access
from .assets import drawing_identity
from .audit import audit

# Which subjects each stage may confirm.
STAGE_SUBJECTS = {
    "design": {"model_revision", "drawing", "intake"},
    "process": {"process_plan"},
    "quote": {"quote"},
}

# Workflow port values -> (subject_type, id key)
PORT_SUBJECT = {
    "model": ("model_revision", "revision_id"),
    "drawing": ("drawing", "drawing_id"),
    "spec": ("intake", "intake_id"),
    "plan": ("process_plan", "process_plan_id"),
    "quote": ("quote", "quote_id"),
}


@dataclass
class Subject:
    type: str
    id: uuid.UUID
    part: Part
    hash: str
    row: object


def _canon(data: object) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def load_subject(db: Session, subject_type: str, subject_id: uuid.UUID) -> Subject:
    model = {
        "model_revision": ModelRevision,
        "drawing": Drawing,
        "intake": Intake,
        "process_plan": ProcessPlan,
        "quote": Quote,
    }.get(subject_type)
    if model is None:
        raise bad_request("unknown_subject", f"cannot approve a {subject_type}")
    row = db.get(model, subject_id)
    if row is None:
        raise not_found(subject_type)
    part = db.get(Part, row.part_id)
    assert part is not None
    if isinstance(row, ModelRevision):
        if row.status != "ready":
            raise conflict("subject_not_ready", "a failed revision cannot be approved")
        h = row.build_key or _canon({"ops": row.ops, "base": row.base, "mesh": row.mesh_sha})
    elif isinstance(row, Drawing):
        # Package rows: the whole manifest (all pages, formats, reports, source binding). Older rows: page 1.
        h = drawing_identity(row) or _canon({"svg": row.svg_sha, "dxf": row.dxf_sha, "file": str(row.file_version_id), "rev": str(row.model_revision_id)})
    elif isinstance(row, Intake):
        h = _canon(row.summary)
    elif isinstance(row, ProcessPlan):
        h = row.inputs_hash
    else:
        h = row.inputs_hash  # type: ignore[union-attr]
    return Subject(type=subject_type, id=subject_id, part=part, hash=h, row=row)


def subject_from_port(value: dict | None) -> tuple[str, uuid.UUID]:
    if not isinstance(value, dict) or value.get("type") not in PORT_SUBJECT:
        raise bad_request("unsupported_subject", "this input cannot be reviewed")
    st, key = PORT_SUBJECT[value["type"]]
    return st, uuid.UUID(str(value[key]))


def latest_decision(db: Session, subject_type: str, subject_id: uuid.UUID, stage: str) -> Approval | None:
    return db.scalar(
        select(Approval)
        .where(Approval.subject_type == subject_type, Approval.subject_id == subject_id, Approval.stage == stage)
        .order_by(Approval.created_at.desc())
        .limit(1)
    )


def decide(
    db: Session,
    user: User,
    *,
    subject_type: str,
    subject_id: uuid.UUID,
    stage: str,
    decision: str,
    comment: str = "",
    expected_hash: str | None = None,
    node_run_id: uuid.UUID | None = None,
    stale_check=None,
) -> Approval:
    """Record a decision after checking role, subject/stage pairing and version.

    `stale_check(db, subject) -> list[str]` returns reasons the subject is out
    of date (e.g. its design revision was superseded); approvals are refused
    while any reason exists. Rejections are always allowed.

    A rejection right after an approval of the same subject and stage revokes
    it: done tasks whose accepted submission delivered that version get an
    impact notice (contract §3.4); the earlier decision is kept as it was.
    """
    if stage not in STAGE_SUBJECTS:
        raise bad_request("unknown_stage", f"unknown stage {stage}")
    if subject_type not in STAGE_SUBJECTS[stage]:
        raise bad_request("stage_subject_mismatch", f"a {stage} review cannot confirm a {subject_type}")
    subj = load_subject(db, subject_type, subject_id)
    acc = project_access(db, user, subj.part.project_id, STAGE_ACTION[stage])
    if expected_hash and expected_hash != subj.hash:
        raise conflict("subject_changed", "the item changed after you opened it; reload and review again")
    if decision == "approved" and stale_check is not None:
        reasons = stale_check(db, subj)
        if reasons:
            raise AppError(409, "subject_stale", "the item is out of date and cannot be approved", {"reasons": reasons})
    previous = latest_decision(db, subject_type, subject_id, stage)
    row = Approval(
        project_id=subj.part.project_id,
        part_id=subj.part.id,
        subject_type=subject_type,
        subject_id=subject_id,
        stage=stage,
        decision=decision,
        comment=comment,
        subject_hash=subj.hash,
        user_id=user.id,
        role=acc.role,
        node_run_id=node_run_id,
    )
    db.add(row)
    audit(
        db,
        action=f"approval.{decision}",
        subject_type=subject_type,
        subject_id=subject_id,
        user_id=user.id,
        project_id=subj.part.project_id,
        data={"stage": stage, "hash": subj.hash, "comment": comment},
    )
    db.flush()
    if decision == "rejected" and previous is not None and previous.decision == "approved":
        from .stages import approval_revoked

        approval_revoked(db, user.id, subject_type=subject_type, subject_id=subject_id, stage=stage)
    return row


def is_approved(db: Session, subject_type: str, subject_id: uuid.UUID, stage: str) -> bool:
    """Latest decision is an approval AND the subject hash is unchanged."""
    a = latest_decision(db, subject_type, subject_id, stage)
    if a is None or a.decision != "approved":
        return False
    try:
        return load_subject(db, subject_type, subject_id).hash == a.subject_hash
    except AppError:
        return False


def decide_as_agent(
    db: Session,
    *,
    agent_profile,
    subject_type: str,
    subject_id: uuid.UUID,
    stage: str,
    decision: str,
    comment: str = "",
    expected_hash: str | None = None,
    node_run_id: uuid.UUID | None = None,
    stale_check=None,
) -> Approval:
    """Engineering approval by an independent review agent of a full-auto
    collaboration run (design §2.1: approvals.actor_kind / agent_id). Same
    subject / stage / hash / staleness rules as `decide`; the agent must be an
    enabled review or general profile of the subject's project. Caller commits."""
    from ..models import AuditLog

    if stage not in STAGE_SUBJECTS:
        raise bad_request("unknown_stage", f"unknown stage {stage}")
    if subject_type not in STAGE_SUBJECTS[stage]:
        raise bad_request("stage_subject_mismatch", f"a {stage} review cannot confirm a {subject_type}")
    if decision not in ("approved", "rejected"):
        raise AppError(422, "unknown_decision", "decision is approved or rejected")
    subj = load_subject(db, subject_type, subject_id)
    if agent_profile.project_id != subj.part.project_id:
        raise not_found(subject_type)
    if not agent_profile.enabled:
        raise AppError(422, "agent_disabled", f"agent {agent_profile.name} is disabled", {"agent_profile_id": str(agent_profile.id)})
    if agent_profile.purpose not in ("review", "general"):
        raise AppError(403, "agent_purpose_mismatch", "only a review agent may record engineering approvals",
                       {"agent_profile_id": str(agent_profile.id), "purpose": agent_profile.purpose})
    if expected_hash and expected_hash != subj.hash:
        raise conflict("subject_changed", "the item changed after it was reviewed")
    if decision == "approved" and stale_check is not None:
        reasons = stale_check(db, subj)
        if reasons:
            raise AppError(409, "subject_stale", "the item is out of date and cannot be approved", {"reasons": reasons})
    previous = latest_decision(db, subject_type, subject_id, stage)
    row = Approval(
        project_id=subj.part.project_id,
        part_id=subj.part.id,
        subject_type=subject_type,
        subject_id=subject_id,
        stage=stage,
        decision=decision,
        comment=comment,
        subject_hash=subj.hash,
        user_id=None,
        actor_kind="agent",
        agent_id=agent_profile.id,
        role="agent",
        node_run_id=node_run_id,
    )
    db.add(row)
    db.add(AuditLog(project_id=subj.part.project_id, user_id=None, agent_id=agent_profile.id, action=f"approval.{decision}",
                    subject_type=subject_type, subject_id=str(subject_id), data={"stage": stage, "hash": subj.hash, "comment": comment}))
    db.flush()
    if decision == "rejected" and previous is not None and previous.decision == "approved":
        from .stages import approval_revoked

        approval_revoked(db, None, subject_type=subject_type, subject_id=subject_id, stage=stage)  # type: ignore[arg-type]
    return row
