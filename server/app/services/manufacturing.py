"""Process plans and quotes, bound to the exact design data they came from."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..domain import FeatureSummary, PlanParams, QuoteParams, compute_quote, plan_process
from ..domain.planning import ProcessPlan as PlanModel
from ..errors import AppError, bad_request, not_found
from ..models import Intake, ModelRevision, Part, ProcessPlan, Quote, User
from ..storage import read_blob
from . import assets
from .audit import audit
from .features import summary_from_revision


def business_today() -> date:
    return datetime.now(ZoneInfo(get_settings().business_timezone)).date()


def latest_revision(db: Session, part_id: uuid.UUID) -> ModelRevision | None:
    return db.scalar(
        select(ModelRevision)
        .where(ModelRevision.part_id == part_id, ModelRevision.status == "ready")
        .order_by(ModelRevision.rev_no.desc())
        .limit(1)
    )


def latest_intake(db: Session, part_id: uuid.UUID) -> Intake | None:
    return db.scalar(select(Intake).where(Intake.part_id == part_id).order_by(Intake.created_at.desc()).limit(1))


def summary_for_revision(db: Session, rev: ModelRevision, *, frozen_material: bool = False) -> FeatureSummary:
    part = db.get(Part, rev.part_id)
    assert part is not None
    step_bytes = None
    if rev.step_sha and any(h.get("through", h.get("depth") is None) and h.get("depth") is None for h in rev.holes or []):
        try:
            step_bytes = read_blob(db, rev.step_sha)[0]
        except FileNotFoundError:
            pass  # Historical/missing geometry keeps a labelled conservative depth estimate.
    return summary_from_revision(rev, rev.material if frozen_material else part.material, step_bytes=step_bytes)


def summary_for_intake(db: Session, intake: Intake) -> FeatureSummary:
    part = db.get(Part, intake.part_id)
    assert part is not None
    data = dict(intake.summary)
    # Part material is the single source of truth once someone sets it.
    data["material"] = part.material or data.get("material")
    return FeatureSummary.model_validate(data)


def create_plan(
    db: Session,
    *,
    part: Part,
    user: User,
    model_revision_id: uuid.UUID | None = None,
    intake_id: uuid.UUID | None = None,
    params: dict | None = None,
    node_run_id: uuid.UUID | None = None,
    frozen_revision_material: bool = False,
) -> ProcessPlan:
    if (model_revision_id is None) == (intake_id is None):
        raise bad_request("plan_source_required", "plan from exactly one model revision or one drawing intake")
    plan_params = PlanParams.model_validate(params or {})
    if model_revision_id:
        rev = db.get(ModelRevision, model_revision_id)
        if rev is None or rev.part_id != part.id:
            raise not_found("model revision")
        if rev.status != "ready":
            raise bad_request("revision_not_ready", "the revision did not build")
        summary = summary_for_revision(db, rev, frozen_material=frozen_revision_material)
    else:
        intake = db.get(Intake, intake_id)
        if intake is None or intake.part_id != part.id:
            raise not_found("intake")
        summary = summary_for_intake(db, intake)
    plan = plan_process(summary, plan_params)
    if frozen_revision_material:
        # Actual geometry is retained, but this route has no confirmed shop process
        # and its time/rate tables are examples. The existing typed estimate travels on.
        plan.estimated = True
        plan.estimated_fields = list(dict.fromkeys([*plan.estimated_fields, "synthetic_demo_rates"]))
    row = ProcessPlan(
        part_id=part.id,
        model_revision_id=model_revision_id,
        intake_id=intake_id,
        inputs_hash=plan.inputs_hash,
        summary=summary.model_dump(mode="json"),
        params=plan_params.model_dump(mode="json"),
        plan=plan.model_dump(mode="json"),
        rules_version=plan.rules_version,
        node_run_id=node_run_id,
        created_by=user.id,
    )
    db.add(row)
    db.flush()
    assets.register_plan(db, row)  # derived_from model revision or intake; estimated label
    audit(db, action="plan.create", subject_type="process_plan", subject_id=row.id, user_id=user.id, project_id=part.project_id, data={"inputs_hash": plan.inputs_hash})
    return row


def create_quote(
    db: Session,
    *,
    plan_row: ProcessPlan,
    user: User,
    params: dict,
    node_run_id: uuid.UUID | None = None,
    synthetic_estimate: bool = False,
) -> Quote:
    data = dict(params)
    data.setdefault("quote_date", business_today().isoformat())
    qp = QuoteParams.model_validate(data)
    plan = PlanModel.model_validate(plan_row.plan)
    summary = FeatureSummary.model_validate(plan_row.summary)
    result = compute_quote(plan, summary, qp)
    if not result.priceable:
        raise AppError(
            422,
            "not_priceable",
            "this plan has blocking gaps and cannot be priced",
            {"reasons": [g.model_dump(mode="json") for g in result.refusal_reasons]},
        )
    stored_result = result.model_dump(mode="json")
    if synthetic_estimate:
        stored_result.update(estimated=True, pricing_basis="synthetic_demo_rates", commercial_status="draft")
    row = Quote(
        part_id=plan_row.part_id,
        process_plan_id=plan_row.id,
        inputs_hash=result.inputs_hash,
        params=qp.model_dump(mode="json"),
        result=stored_result,
        currency=result.currency,
        node_run_id=node_run_id,
        created_by=user.id,
    )
    db.add(row)
    db.flush()
    assets.register_quote(db, row)  # priced_from the plan; estimated label
    part = db.get(Part, plan_row.part_id)
    audit(db, action="quote.create", subject_type="quote", subject_id=row.id, user_id=user.id, project_id=part.project_id if part else None, data={"inputs_hash": result.inputs_hash})
    return row


def plan_out(row: ProcessPlan) -> dict:
    return {
        "id": str(row.id),
        "part_id": str(row.part_id),
        "model_revision_id": str(row.model_revision_id) if row.model_revision_id else None,
        "intake_id": str(row.intake_id) if row.intake_id else None,
        "inputs_hash": row.inputs_hash,
        "summary": row.summary,
        "params": row.params,
        "plan": row.plan,
        "rules_version": row.rules_version,
        "node_run_id": str(row.node_run_id) if row.node_run_id else None,
        "created_by": str(row.created_by),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def quote_out(row: Quote) -> dict:
    return {
        "id": str(row.id),
        "part_id": str(row.part_id),
        "process_plan_id": str(row.process_plan_id),
        "inputs_hash": row.inputs_hash,
        "params": row.params,
        "result": row.result,
        "currency": row.currency,
        "node_run_id": str(row.node_run_id) if row.node_run_id else None,
        "created_by": str(row.created_by),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
