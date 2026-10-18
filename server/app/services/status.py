"""Part status: current design version, downstream staleness, confirmations.

Staleness is decided by recomputing inputs hashes from the part's CURRENT
design data (latest ready revision, or latest drawing intake for drawing-only
parts) and comparing with the stored hash. Reasons come from the domain's
summary diff, so a layout-only revision (same manufacturing summary) does not
invalidate plans or quotes, while a hole or material change does.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain import FeatureSummary, PlanParams, QuoteParams, evaluate, plan_inputs_hash, quote_inputs_hash
from ..models import Drawing, Intake, ModelRevision, Part, ProcessPlan, Quote
from . import approvals
from .manufacturing import latest_intake, latest_revision, summary_for_intake, summary_for_revision


def current_summary_for_plan(db: Session, plan: ProcessPlan) -> tuple[FeatureSummary | None, dict[str, Any]]:
    """The summary the plan WOULD be computed from today, and where it comes from."""
    if plan.model_revision_id is not None:
        rev = latest_revision(db, plan.part_id)
        if rev is None:
            return None, {}
        return summary_for_revision(db, rev), {"model_revision_id": str(rev.id), "rev_no": rev.rev_no}
    intake = latest_intake(db, plan.part_id)
    if intake is None:
        return None, {}
    return summary_for_intake(db, intake), {"intake_id": str(intake.id)}


def plan_staleness(db: Session, plan: ProcessPlan) -> dict[str, Any]:
    current, source = current_summary_for_plan(db, plan)
    stored = FeatureSummary.model_validate(plan.summary)
    current_hash = plan_inputs_hash(current, PlanParams.model_validate(plan.params)) if current else ""
    st = evaluate(plan.inputs_hash, current_hash, stored, current)
    return {"stale": st.stale, "manufacturing": st.manufacturing, "reasons": [r.model_dump(mode="json") for r in st.reasons], "current": source}


def quote_staleness(db: Session, quote: Quote) -> dict[str, Any]:
    plan = db.get(ProcessPlan, quote.process_plan_id)
    assert plan is not None
    current, source = current_summary_for_plan(db, plan)
    stored = FeatureSummary.model_validate(plan.summary)
    current_hash = (
        quote_inputs_hash(current, PlanParams.model_validate(plan.params), QuoteParams.model_validate(quote.params)) if current else ""
    )
    st = evaluate(quote.inputs_hash, current_hash, stored, current)
    return {"stale": st.stale, "manufacturing": st.manufacturing, "reasons": [r.model_dump(mode="json") for r in st.reasons], "current": source}


def _reason(code: str, en: str, zh: str) -> dict[str, Any]:
    return {"field": code, "manufacturing": False, "message_en": en, "message_zh": zh}


def stale_reasons(db: Session, subject: approvals.Subject) -> list[dict[str, Any]]:
    """Why a subject may not be approved now (empty list = OK)."""
    row = subject.row
    if isinstance(row, ModelRevision):
        latest = latest_revision(db, row.part_id)
        if latest is not None and latest.id != row.id:
            return [_reason("newer_revision", f"R{latest.rev_no} is newer than R{row.rev_no}", f"已有更新的修订 R{latest.rev_no}")]
        return []
    if isinstance(row, Intake):
        latest = latest_intake(db, row.part_id)
        if latest is not None and latest.id != row.id:
            return [_reason("newer_intake", "a newer drawing intake exists", "已有更新的图纸整理结果")]
        return []
    if isinstance(row, Drawing):
        if row.model_revision_id:
            latest = latest_revision(db, row.part_id)
            if latest is not None and latest.id != row.model_revision_id:
                return [_reason("newer_revision", f"the drawing was generated from an older revision; R{latest.rev_no} exists", f"图纸来自旧修订，已有 R{latest.rev_no}")]
        return []
    if isinstance(row, ProcessPlan):
        st = plan_staleness(db, row)
        reasons = st["reasons"] if st["stale"] else []
        if not reasons and not _design_confirmed_for_plan(db, row):
            reasons.append(_reason("design_not_confirmed", "confirm the design (or drawing intake) first", "请先确认设计（或图纸整理结果）"))
        return reasons
    if isinstance(row, Quote):
        st = quote_staleness(db, row)
        reasons = st["reasons"] if st["stale"] else []
        if not reasons and not approvals.is_approved(db, "process_plan", row.process_plan_id, "process"):
            reasons.append(_reason("process_not_confirmed", "confirm the process plan first", "请先确认工艺计划"))
        return reasons
    return []


def _design_confirmed_for_plan(db: Session, plan: ProcessPlan) -> bool:
    if plan.model_revision_id:
        return approvals.is_approved(db, "model_revision", plan.model_revision_id, "design")
    if plan.intake_id:
        return approvals.is_approved(db, "intake", plan.intake_id, "design")
    return False


def _approval_view(db: Session, subject_type: str, subject_id: uuid.UUID, stage: str) -> dict[str, Any] | None:
    a = approvals.latest_decision(db, subject_type, subject_id, stage)
    if a is None:
        return None
    return {
        "decision": a.decision,
        "valid": approvals.is_approved(db, subject_type, subject_id, stage) if a.decision == "approved" else False,
        "comment": a.comment,
        "user_id": str(a.user_id) if a.user_id else None,
        "actor_kind": a.actor_kind,
        "agent_id": str(a.agent_id) if a.agent_id else None,
        "role": a.role,
        "at": a.created_at.isoformat(),
    }


def part_status(db: Session, part: Part) -> dict[str, Any]:
    rev = latest_revision(db, part.id)
    intake = latest_intake(db, part.id)
    design: dict[str, Any] | None = None
    if rev is not None:
        design = {
            "kind": "model_revision",
            "id": str(rev.id),
            "rev_no": rev.rev_no,
            "mesh_only": rev.mesh_only,
            "approval": _approval_view(db, "model_revision", rev.id, "design"),
        }
    elif intake is not None:
        design = {"kind": "intake", "id": str(intake.id), "approval": _approval_view(db, "intake", intake.id, "design")}

    drawing = None
    if rev is not None:
        d = db.scalar(select(Drawing).where(Drawing.part_id == part.id).order_by(Drawing.created_at.desc()).limit(1))
        if d is not None:
            drawing = {"id": str(d.id), "model_revision_id": str(d.model_revision_id) if d.model_revision_id else None,
                       "stale": bool(d.model_revision_id and d.model_revision_id != rev.id)}

    plan_row = db.scalar(select(ProcessPlan).where(ProcessPlan.part_id == part.id).order_by(ProcessPlan.created_at.desc()).limit(1))
    plan = None
    if plan_row is not None:
        plan = {"id": str(plan_row.id), **plan_staleness(db, plan_row), "approval": _approval_view(db, "process_plan", plan_row.id, "process"),
                "priceable": bool(plan_row.plan.get("priceable")), "estimated": bool(plan_row.plan.get("estimated"))}

    quote_row = db.scalar(select(Quote).where(Quote.part_id == part.id).order_by(Quote.created_at.desc()).limit(1))
    quote = None
    if quote_row is not None:
        quote = {"id": str(quote_row.id), "process_plan_id": str(quote_row.process_plan_id), **quote_staleness(db, quote_row),
                 "approval": _approval_view(db, "quote", quote_row.id, "quote"), "estimated": bool(quote_row.result.get("estimated"))}

    blockers: list[str] = []
    if design is None:
        blockers.append("no_design")
    elif not (design["approval"] and design["approval"]["valid"]):
        blockers.append("design_not_confirmed")
    if plan is None:
        blockers.append("no_plan")
    else:
        if plan["stale"]:
            blockers.append("plan_stale")
        if not (plan["approval"] and plan["approval"]["valid"]):
            blockers.append("plan_not_confirmed")
    if quote is None:
        blockers.append("no_quote")
    else:
        if quote["stale"]:
            blockers.append("quote_stale")
        if not (quote["approval"] and quote["approval"]["valid"]):
            blockers.append("quote_not_confirmed")
        if plan is not None and quote["process_plan_id"] != plan["id"]:
            blockers.append("quote_not_from_latest_plan")
    return {
        "part_id": str(part.id),
        "design": design,
        "drawing": drawing,
        "plan": plan,
        "quote": quote,
        # A quote is "formal" only when every confirmation holds on current data.
        "formal": not blockers,
        "blockers": blockers,
    }
