"""Asset catalogue API (contract §4): unified listing, versions, provenance,
usages and publish state. Engineering behaviour stays in the specialised APIs."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import not_found
from ..models import (
    ApprovalRequest,
    Asset,
    AssetVersion,
    File,
    KnowledgeCollection,
    KnowledgeCollectionVersion,
    KnowledgeItem,
    KnowledgeUsage,
    Part,
    ProjectMember,
    RunInputBinding,
    RunOutput,
    SubmissionItem,
    Task,
    TaskBaselineItem,
    TaskBaselineSnapshot,
    TaskDeliverableDraft,
    TaskSubmission,
    User,
    WorkflowRun,
)
from ..security import current_user, project_access
from ..services import assets, collaboration_resources, knowledge

router = APIRouter(tags=["assets"])


class PublishIn(BaseModel):
    state: Literal["published", "deprecated"]


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _load_asset(db: Session, user: User, asset_id: uuid.UUID) -> Asset:
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise not_found("asset")
    project_access(db, user, asset.project_id)
    return asset


def _load_version(db: Session, user: User, version_id: uuid.UUID, action: str = "view") -> tuple[AssetVersion, Asset]:
    v = assets.get_version(db, version_id)
    asset = db.get(Asset, v.asset_id)
    assert asset is not None
    project_access(db, user, asset.project_id, action)
    return v, asset


@router.get("/projects/{project_id}/assets")
def list_assets(
    project_id: uuid.UUID,
    type: str | None = None,
    q: str | None = None,
    label: str | None = None,
    part_id: uuid.UUID | None = None,
    include_archived: bool = False,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    project_access(db, user, project_id)
    stmt = (
        select(Asset)
        .outerjoin(Part, Part.id == Asset.part_id)
        .outerjoin(File, File.id == Asset.file_id)
        .where(Asset.project_id == project_id)
        .order_by(Asset.created_at.desc(), Asset.id)
    )
    if type:
        stmt = stmt.where(Asset.type == type)
    if part_id:
        stmt = stmt.where(Asset.part_id == part_id)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Asset.name.ilike(like), Part.number.ilike(like), Part.name.ilike(like), File.name.ilike(like)))
    if not include_archived:
        stmt = stmt.where(Asset.archived_at.is_(None), Part.archived_at.is_(None), File.archived_at.is_(None))
    out = []
    rows = db.scalars(stmt).all()
    pre = assets.prefetch_listing(db, rows)
    for asset in rows:
        item = assets.asset_out(db, asset, latest=pre.latest.get(asset.id), count=pre.counts.get(asset.id, 0))
        if label and (item["latest_version"] is None or label not in item["latest_version"]["labels"]):
            continue
        out.append(item)
    return out


@router.get("/assets/{asset_id}")
def get_asset(asset_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return assets.asset_out(db, _load_asset(db, user, asset_id))


@router.get("/assets/{asset_id}/versions")
def list_versions(asset_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    asset = _load_asset(db, user, asset_id)
    rows = db.scalars(select(AssetVersion).where(AssetVersion.asset_id == asset.id).order_by(AssetVersion.version_no.desc())).all()
    return [assets.version_out(db, v) for v in rows]


@router.get("/asset-versions/{version_id}")
def get_version(version_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    v, _ = _load_version(db, user, version_id)
    return assets.version_out(db, v)


UsageRelation = Literal["all", "workflow_resource", "knowledge_item", "knowledge_usage"]


def _knowledge_relations(db: Session, user: User, v: AssetVersion, *, relation: str, limit: int, offset: int) -> dict:
    """C11: workflow resources, knowledge items and knowledge usages of this exact version,
    each with its own page of the same (limit, offset) window; relations whose source or
    target project the user cannot see are left out."""
    visible = set(db.scalars(select(ProjectMember.project_id).where(ProjectMember.user_id == user.id)).all())
    want = {"workflow_resource", "knowledge_item", "knowledge_usage"} if relation == "all" else {relation}
    resources: list[dict] = []
    resources_total = 0
    if "workflow_resource" in want:
        page = collaboration_resources.page(db, {"asset_version_id": v.id, "project_ids": visible}, limit=limit, offset=offset)
        resources, resources_total = page["items"], page["total"]
    items: list[dict] = []
    items_total = 0
    if "knowledge_item" in want:
        base = (
            select(KnowledgeItem, KnowledgeCollectionVersion, KnowledgeCollection)
            .join(KnowledgeCollectionVersion, KnowledgeCollectionVersion.id == KnowledgeItem.collection_version_id)
            .join(KnowledgeCollection, KnowledgeCollection.id == KnowledgeCollectionVersion.collection_id)
            .where(KnowledgeItem.asset_version_id == v.id, KnowledgeCollection.project_id.in_(list(visible)))
        )
        items_total = int(db.scalar(select(func.count()).select_from(base.subquery())) or 0)
        for item, ver, col in db.execute(base.order_by(KnowledgeItem.created_at.desc(), KnowledgeItem.id.desc()).limit(limit).offset(offset)).all():
            items.append({
                "item_id": str(item.id), "collection_id": str(col.id), "collection_title": col.title, "collection_status": col.status,
                "collection_version_id": str(ver.id), "version_no": ver.version_no, "current": col.current_version_id == ver.id,
                "resource_id": str(item.resource_id) if item.resource_id else None, "asset_version_id": str(item.asset_version_id),
                "content_hash": item.content_hash, "run_id": str(item.run_id) if item.run_id else None,
                "node_task_id": str(item.node_task_id) if item.node_task_id else None, "task_id": str(item.task_id) if item.task_id else None,
                "slot_key": item.slot_key, "kind": item.kind, "reusable": item.reusable, "index_status": item.index_status,
                "created_at": _iso(item.created_at),
            })
    usages_rows: list[dict] = []
    usages_total = 0
    if "knowledge_usage" in want:
        base = (
            select(KnowledgeUsage)
            .join(KnowledgeCollectionVersion, KnowledgeCollectionVersion.id == KnowledgeUsage.collection_version_id)
            .join(KnowledgeCollection, KnowledgeCollection.id == KnowledgeCollectionVersion.collection_id)
            .where(KnowledgeUsage.asset_version_id == v.id, KnowledgeUsage.target_project_id.in_(list(visible)),
                   KnowledgeCollection.project_id.in_(list(visible)))
        )
        usages_total = int(db.scalar(select(func.count()).select_from(base.subquery())) or 0)
        rows = list(db.scalars(base.order_by(KnowledgeUsage.created_at.desc(), KnowledgeUsage.id.desc()).limit(limit).offset(offset)).all())
        usages_rows = knowledge.usages_out(db, rows)
    return {
        "workflow_resources": resources,
        "knowledge_items": items,
        "knowledge_usages": usages_rows,
        "pagination": {
            "workflow_resources": {"total": resources_total, "limit": limit, "offset": offset},
            "knowledge_items": {"total": items_total, "limit": limit, "offset": offset},
            "knowledge_usages": {"total": usages_total, "limit": limit, "offset": offset},
        },
    }


@router.get("/asset-versions/{version_id}/usages")
def usages(
    version_id: uuid.UUID,
    relation: UsageRelation = "all",
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Where this exact version is used: task baselines, deliverable drafts,
    submissions, run inputs and outputs, and approval requests on it (unchanged),
    plus the paged workflow-resource / knowledge-item / knowledge-usage relations (C11)."""
    v, _ = _load_version(db, user, version_id)

    def task_ref(t: Task) -> dict:
        return {"task_id": str(t.id), "task_number": t.number, "task_title": t.title, "task_status": t.status}

    baselines = [
        {**task_ref(t), "snapshot_id": str(s.id), "snapshot_seq": s.seq, "slot_key": i.slot_key,
         "current": t.current_baseline_id == s.id, "adopted_at": _iso(s.adopted_at)}
        for i, s, t in db.execute(
            select(TaskBaselineItem, TaskBaselineSnapshot, Task)
            .join(TaskBaselineSnapshot, TaskBaselineSnapshot.id == TaskBaselineItem.snapshot_id)
            .join(Task, Task.id == TaskBaselineSnapshot.task_id)
            .where(TaskBaselineItem.asset_version_id == v.id)
            .order_by(TaskBaselineSnapshot.adopted_at)
        ).all()
    ]
    deliverables = [
        {**task_ref(t), "id": str(d.id), "criterion_ids": [str(c) for c in d.criterion_ids or []], "note": d.note,
         "selected_by": str(d.selected_by), "selected_at": _iso(d.selected_at)}
        for d, t in db.execute(
            select(TaskDeliverableDraft, Task).join(Task, Task.id == TaskDeliverableDraft.task_id)
            .where(TaskDeliverableDraft.asset_version_id == v.id).order_by(TaskDeliverableDraft.selected_at)
        ).all()
    ]
    submissions = [
        {**task_ref(t), "submission_id": str(s.id), "seq": s.seq, "status": s.status, "item_id": str(i.id),
         "criterion_id": str(i.criterion_id) if i.criterion_id else None, "content_hash": i.content_hash,
         "submitted_at": _iso(s.submitted_at)}
        for i, s, t in db.execute(
            select(SubmissionItem, TaskSubmission, Task)
            .join(TaskSubmission, TaskSubmission.id == SubmissionItem.submission_id)
            .join(Task, Task.id == TaskSubmission.task_id)
            .where(SubmissionItem.asset_version_id == v.id)
            .order_by(TaskSubmission.submitted_at)
        ).all()
    ]

    def run_ref(r: WorkflowRun) -> dict:
        return {"run_id": str(r.id), "run_title": r.title, "run_status": r.status, "task_id": str(r.task_id) if r.task_id else None,
                "created_at": _iso(r.created_at)}

    run_inputs = [
        {**run_ref(r), "port_key": b.port_key, "content_hash": b.content_hash}
        for b, r in db.execute(
            select(RunInputBinding, WorkflowRun).join(WorkflowRun, WorkflowRun.id == RunInputBinding.run_id)
            .where(RunInputBinding.asset_version_id == v.id).order_by(WorkflowRun.created_at)
        ).all()
    ]
    run_outputs = [
        {**run_ref(r), "node_run_id": str(o.node_run_id), "attempt": o.attempt, "output_key": o.output_key}
        for o, r in db.execute(
            select(RunOutput, WorkflowRun).join(WorkflowRun, WorkflowRun.id == RunOutput.run_id)
            .where(RunOutput.asset_version_id == v.id).order_by(WorkflowRun.created_at)
        ).all()
    ]
    requests = [
        {"id": str(a.id), "kind": a.kind, "stage": a.stage, "status": a.status, "title": a.title,
         "run_id": str(a.run_id) if a.run_id else None, "task_id": str(a.task_id) if a.task_id else None, "created_at": _iso(a.created_at)}
        for a in db.scalars(select(ApprovalRequest).where(ApprovalRequest.asset_version_id == v.id).order_by(ApprovalRequest.created_at)).all()
    ]
    return {
        "version_id": str(v.id),
        "baselines": baselines,
        "deliverables": deliverables,
        "submissions": submissions,
        "run_inputs": run_inputs,
        "run_outputs": run_outputs,
        "approval_requests": requests,
        **_knowledge_relations(db, user, v, relation=relation, limit=limit, offset=offset),
    }


@router.post("/asset-versions/{version_id}/publish")
def publish(version_id: uuid.UUID, body: PublishIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """Publish or deprecate one version. The role follows the asset type
    (part_model / drawing / intake / document → design, process_plan /
    process_module → process, quote → quote); publishing an engineering version
    also needs a currently valid engineering approval of the matching stage
    (a process module: its latest closed review approved this exact content).
    Publishing is not task acceptance."""
    v = assets.publish_version(db, user, version_id, state=body.state)
    db.commit()
    return assets.version_out(db, v)
