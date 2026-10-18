"""Knowledge archive, index and reuse (design §2.3, §3.11, §4 C09). Backend E owns this module.

A completed collaboration run is archived into one knowledge collection whose
manifest versions are immutable (the same manifest never makes a new version).
Items reference exact asset versions; only currently valid formal outputs of the
latest manifest are reusable by default. Indexing is asynchronous (outbox event
`knowledge.index`), structured only (no vectors) and never changes the manifest,
the hashes or any acceptance. Reuse is a usage record: a reference changes
nothing; a baseline adoption fixes the source and, in a semi-auto instance,
needs the node's human reviewer to confirm. Adoption never copies approvals.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Text, cast, exists, func, or_, select, update
from sqlalchemy.orm import Session, aliased

from .. import storage
from ..errors import AppError, conflict, not_found
from ..models import (
    KNOWLEDGE_USAGES,
    AcceptanceCriterion,
    AgentExecution,
    AgentProfile,
    ApprovalDecision,
    ApprovalRequest,
    Asset,
    AssetVersion,
    Blob,
    CollaborationNodeTask,
    CollaborationRun,
    KnowledgeCollection,
    KnowledgeCollectionVersion,
    KnowledgeItem,
    KnowledgeUsage,
    Project,
    ProjectMember,
    Task,
    TaskBaselineSnapshot,
    TaskSubmission,
    User,
    WorkflowResource,
    WorkflowRun,
)
from ..security import ProjectAccess, project_access
from . import actors, assets, collaboration_resources, outbox
from .actors import ActorRef
from .audit import audit
from . import feature_flags as features
from .tasks import SLOT_RE, canon_hash, criterion_out

log = logging.getLogger("partroute.knowledge")

ARCHIVE_EVENT = "collaboration.completed"
INDEX_EVENT = "knowledge.index"
MAX_TEXT_CHARS = 200_000
TEXT_CONTENT_TYPES = frozenset({"application/json", "application/xml", "application/x-yaml", "application/yaml", "text/markdown", "application/javascript"})
TEXT_EXTENSIONS = (".md", ".markdown", ".txt", ".json", ".csv", ".yaml", ".yml", ".xml", ".rst", ".log")
# Resource kind -> knowledge item kind (ITEM_KINDS has no `input`: inputs are kept as supporting evidence).
ITEM_KIND_OF = {"formal": "formal", "superseded": "history", "candidate": "candidate", "failed": "failed", "evidence": "evidence", "input": "evidence"}


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _s(value: Any) -> str | None:
    return str(value) if value is not None else None


def _actor_json(ref: ActorRef | None) -> dict[str, Any] | None:
    return ref.model_dump(mode="json") if ref is not None else None


# --- completion hook and archive --------------------------------------------------------------


def on_run_completed(db: Session, run: WorkflowRun) -> None:
    """Called by the agent runtime (backend D) when every node of a collaboration run is
    accepted: marks the archive pending and emits `collaboration.completed`; the outbox
    dispatcher performs the archive in its own transaction."""
    cr = db.get(CollaborationRun, run.id)
    if cr is None:
        log.info("run %s completed but has no collaboration metadata; nothing to archive", run.id)
        return
    if cr.execution_policy == "turbo":  # W7: Turbo outputs are never archived as accepted knowledge (AC-T07)
        log.info("Turbo run %s is never archived as accepted knowledge", run.id)
        return
    if cr.completed_at is None:
        cr.completed_at = _now()
    if not features.enabled("collaboration"):
        return  # Completion/audit survives the freeze; no new archive or index work.
    if cr.archive_status != "archived":
        cr.archive_status = "pending"
        cr.archive_error = None
    outbox.emit(
        db, kind=ARCHIVE_EVENT, project_id=run.project_id,
        payload={"run_id": run.id, "actor_user_id": run.created_by, "completed_at": _iso(cr.completed_at)},
        dedupe_key=f"{ARCHIVE_EVENT}:{run.id}:{_iso(cr.completed_at)}",
    )


def build_manifest(db: Session, run: WorkflowRun, cr: CollaborationRun, node_tasks: list[CollaborationNodeTask],
                   resources: list[WorkflowResource]) -> dict[str, Any]:
    """Deterministic archive manifest (§4 C09): demand, frozen contract and graph, criteria, actors,
    submissions, decisions, every resource row and the formal output list."""
    task_ids = [nt.task_id for nt in node_tasks]
    tasks = {t.id: t for t in db.scalars(select(Task).where(Task.id.in_(task_ids))).all()} if task_ids else {}
    criteria: dict[uuid.UUID, list[dict]] = {}
    if task_ids:
        for c in db.scalars(select(AcceptanceCriterion).where(AcceptanceCriterion.task_id.in_(task_ids)).order_by(AcceptanceCriterion.seq)).all():
            criteria.setdefault(c.task_id, []).append(criterion_out(c))
    subs = list(db.scalars(select(TaskSubmission).where(TaskSubmission.task_id.in_(task_ids)).order_by(TaskSubmission.task_id, TaskSubmission.seq)).all()) if task_ids else []
    sub_ids = [s.id for s in subs]
    reqs = list(db.scalars(select(ApprovalRequest).where(ApprovalRequest.submission_id.in_(sub_ids), ApprovalRequest.kind == "task_acceptance")
                           .order_by(ApprovalRequest.created_at, ApprovalRequest.id)).all()) if sub_ids else []
    req_sub = {r.id: r.submission_id for r in reqs}
    decs = list(db.scalars(select(ApprovalDecision).where(ApprovalDecision.request_id.in_(list(req_sub)))
                           .order_by(ApprovalDecision.created_at, ApprovalDecision.id)).all()) if req_sub else []
    node_of_task = {nt.task_id: nt.node_id for nt in node_tasks}
    node_of_nt = {nt.id: nt.node_id for nt in node_tasks}
    contract = cr.contract or {}
    nodes = []
    formal = []
    for nt in sorted(node_tasks, key=lambda x: x.node_id):
        t = tasks.get(nt.task_id)
        nodes.append({
            "node_id": nt.node_id, "task_id": str(nt.task_id), "task_key": f"T-{t.number}" if t else None, "title": t.title if t else None,
            "phase": nt.phase, "accepted_submission_id": _s(nt.accepted_submission_id),
            "bound_inputs": nt.bound_inputs or [], "formal_outputs": nt.formal_outputs or [], "output_slots": nt.output_slots or [],
            "input_slots": nt.input_slots or [],
        })
        for f in sorted((nt.formal_outputs or []), key=lambda x: str(x.get("slot_key"))):
            formal.append({"node_id": nt.node_id, "slot_key": f.get("slot_key"), "asset_version_id": _s(f.get("asset_version_id")),
                           "content_hash": f.get("content_hash")})
    return {
        "schema": 1,
        "run_id": str(run.id),
        "project_id": str(run.project_id),
        "title": run.title,
        "demand": contract.get("demand", ""),
        "execution_mode": cr.execution_mode,
        "contract_hash": cr.contract_hash,
        "contract": contract,
        "graph": run.graph_snapshot or {},
        "template_version_id": _s(cr.template_version_id),
        "criteria": {nt.node_id: criteria.get(nt.task_id, []) for nt in node_tasks},
        "actors": {
            nt.node_id: {
                "executor": _actor_json(actors.read(nt, "executor")),
                "reviewer": _actor_json(actors.read(nt, "reviewer")),
                "pre_reviewer_agent_id": _s(nt.pre_reviewer_agent_id),
            }
            for nt in node_tasks
        },
        "nodes": nodes,
        "submissions": [
            {"id": str(s.id), "node_id": node_of_task.get(s.task_id), "task_id": str(s.task_id), "seq": s.seq, "status": s.status,
             "snapshot_hash": s.snapshot_hash, "baseline_snapshot_id": _s(s.baseline_snapshot_id),
             "submitted_by": (actors.read_legacy(s, "submitted_by").model_dump(mode="json") if actors.read_legacy(s, "submitted_by") else None),
             "submitted_at": _iso(s.submitted_at), "decided_at": _iso(s.decided_at)}
            for s in subs
        ],
        "decisions": [
            {"id": str(d.id), "request_id": str(d.request_id), "submission_id": _s(req_sub.get(d.request_id)), "decision": d.decision,
             "actor": (actors.read_legacy(d, "actor_id").model_dump(mode="json") if actors.read_legacy(d, "actor_id") else None),
             "role": d.role, "target_hash": d.target_hash, "criterion_decisions": d.criterion_decisions or [], "comment": d.comment,
             "created_at": _iso(d.created_at)}
            for d in decs
        ],
        "resources": [
            {"id": str(r.id), "node_task_id": _s(r.node_task_id), "node_id": node_of_nt.get(r.node_task_id), "node_run_id": _s(r.node_run_id),
             "agent_execution_id": _s(r.agent_execution_id), "submission_id": _s(r.submission_id), "attempt": r.attempt, "slot_key": r.slot_key,
             "asset_version_id": str(r.asset_version_id), "content_hash": r.content_hash, "kind": r.kind, "metadata": r.meta or {},
             "created_at": _iso(r.created_at)}
            for r in resources
        ],
        "formal": formal,
    }


def _collection_title(run: WorkflowRun, cr: CollaborationRun) -> str:
    demand = str((cr.contract or {}).get("demand") or "").strip()
    return (run.title or demand[:120] or f"Collaboration {str(run.id)[:8]}")[:300]


def archive(db: Session, run_id: uuid.UUID, *, actor: ActorRef) -> KnowledgeCollectionVersion:
    """Archive a completed collaboration run (idempotent per manifest hash). Caller commits.

    409 `run_not_completed` unless the run succeeded and every node is accepted. A failure
    is recorded by the dispatcher on collaboration_runs (archive_status=failed) and never
    undoes the acceptance."""
    features.require_enabled("collaboration")
    from ..workflow import engine

    run = engine.lock_run(db, run_id)
    cr = db.get(CollaborationRun, run_id)
    if cr is None:
        raise not_found("collaboration run")
    if cr.execution_policy == "turbo":  # W7 (AC-T07): unverified outputs never become accepted, reusable knowledge
        raise conflict("turbo_not_archivable", "a Turbo run is not verified or accepted; it is never archived as accepted knowledge",
                       run_id=str(run.id))
    node_tasks = list(db.scalars(select(CollaborationNodeTask).where(CollaborationNodeTask.run_id == run.id).order_by(CollaborationNodeTask.node_id)).all())
    not_done = {nt.node_id: nt.phase for nt in node_tasks if nt.phase != "accepted"}
    if run.status != "succeeded" or not node_tasks or not_done:
        raise conflict("run_not_completed", "only a completed instance with every node accepted can be archived",
                       run_status=run.status, nodes=not_done)
    resources = list(db.scalars(select(WorkflowResource).where(WorkflowResource.run_id == run.id)
                                .order_by(WorkflowResource.created_at, WorkflowResource.id)).all())
    manifest = build_manifest(db, run, cr, node_tasks, resources)
    manifest_hash = canon_hash(manifest)

    col = db.scalar(select(KnowledgeCollection).where(KnowledgeCollection.run_id == run.id).with_for_update())
    if col is None:
        col = KnowledgeCollection(
            project_id=run.project_id, run_id=run.id, title=_collection_title(run, cr),
            scope={"project_id": str(run.project_id), "run_id": str(run.id), "execution_mode": cr.execution_mode,
                   "part_id": _s(run.part_id), "node_count": len(node_tasks)},
            status="active", index_status="pending",
        )
        actors.assign_legacy(col, "created_by", actor)
        db.add(col)
        db.flush()
    existing = db.scalar(select(KnowledgeCollectionVersion).where(KnowledgeCollectionVersion.collection_id == col.id,
                                                                  KnowledgeCollectionVersion.manifest_hash == manifest_hash))
    if existing is not None:
        if col.current_version_id is None:
            col.current_version_id = existing.id
        cr.archive_status, cr.archive_error, cr.knowledge_collection_id = "archived", None, col.id
        db.flush()
        return existing

    version_no = int(db.scalar(select(func.max(KnowledgeCollectionVersion.version_no)).where(KnowledgeCollectionVersion.collection_id == col.id)) or 0) + 1
    ver = KnowledgeCollectionVersion(collection_id=col.id, version_no=version_no, manifest=manifest, manifest_hash=manifest_hash)
    db.add(ver)
    db.flush()
    # Only the latest manifest holds the default reusable set (§3.3).
    older = select(KnowledgeCollectionVersion.id).where(KnowledgeCollectionVersion.collection_id == col.id, KnowledgeCollectionVersion.id != ver.id)
    db.execute(update(KnowledgeItem).where(KnowledgeItem.collection_version_id.in_(older), KnowledgeItem.reusable.is_(True)).values(reusable=False))

    nt_by_id = {nt.id: nt for nt in node_tasks}
    superseded_keys = {(r.node_task_id, r.slot_key, r.asset_version_id, r.submission_id) for r in resources if r.kind == "superseded"}
    formal_hash: dict[tuple, str] = {}
    nodes_with_formal: set[uuid.UUID] = set()
    for nt in node_tasks:
        for f in nt.formal_outputs or []:
            try:
                formal_hash[(nt.id, f.get("slot_key"), uuid.UUID(str(f.get("asset_version_id"))))] = str(f.get("content_hash"))
                nodes_with_formal.add(nt.id)
            except (ValueError, AttributeError):
                continue
    counts = {"formal": 0, "history": 0, "candidate": 0, "failed": 0, "evidence": 0}
    for r in resources:
        key = (r.node_task_id, r.slot_key, r.asset_version_id)
        kind = ITEM_KIND_OF.get(r.kind, "evidence")
        reusable = False
        if r.kind == "formal":
            nt_row = nt_by_id.get(r.node_task_id) if r.node_task_id else None
            current_sub = nt_row is None or nt_row.accepted_submission_id is None or nt_row.accepted_submission_id == r.submission_id
            valid = ((*key, r.submission_id) not in superseded_keys and current_sub
                     and (r.node_task_id not in nodes_with_formal or formal_hash.get(key) == r.content_hash))
            kind, reusable = ("formal", True) if valid else ("history", False)
        nt = nt_by_id.get(r.node_task_id) if r.node_task_id else None
        db.add(KnowledgeItem(
            collection_version_id=ver.id, resource_id=r.id, asset_version_id=r.asset_version_id, content_hash=r.content_hash,
            run_id=r.run_id, node_task_id=r.node_task_id, task_id=nt.task_id if nt else None, slot_key=r.slot_key,
            kind=kind, reusable=reusable, index_status="pending",
        ))
        counts[kind] += 1
    col.current_version_id = ver.id
    col.scope = {**(col.scope or {}), "node_count": len(node_tasks), "execution_mode": cr.execution_mode}
    cr.archive_status, cr.archive_error, cr.knowledge_collection_id = "archived", None, col.id
    audit(db, action="knowledge.archive", subject_type="knowledge_collection", subject_id=col.id, user_id=actor.user_id, project_id=run.project_id,
          data={"run_id": str(run.id), "version_id": str(ver.id), "version_no": version_no, "manifest_hash": manifest_hash, "counts": counts,
                "actor": actor.model_dump(mode="json")})
    db.flush()
    queue_index(db, col, reason="archive", version_id=ver.id)
    db.flush()
    return ver


def record_archive_failure(db: Session, run_id: uuid.UUID, error: str) -> None:
    cr = db.get(CollaborationRun, run_id)
    if cr is not None and cr.archive_status != "archived":
        cr.archive_status = "failed"
        cr.archive_error = error[:2000]


# --- index ------------------------------------------------------------------------------------


def queue_index(db: Session, col: KnowledgeCollection, *, reason: str, version_id: uuid.UUID | None = None) -> None:
    """Mark the collection pending and emit `knowledge.index` (the dispatcher runs `run_index`)."""
    features.require_enabled("collaboration")
    col.index_status = "pending"
    col.index_error = None
    key = f"{INDEX_EVENT}:{col.id}:{version_id}" if reason == "archive" and version_id else f"{INDEX_EVENT}:{col.id}:{reason}:{uuid.uuid4()}"
    outbox.emit(db, kind=INDEX_EVENT, project_id=col.project_id,
                payload={"collection_id": col.id, "version_id": version_id or col.current_version_id, "reason": reason}, dedupe_key=key)


def _extract_text(db: Session, item: KnowledgeItem) -> tuple[str, str | None, str | None]:
    """(index_status, extracted_text, error) for one item. Text-like document blobs are read
    (≤ 200k chars); pdf / binary documents are `not_applicable`; specialised rows contribute
    their structured summary."""
    v = db.get(AssetVersion, item.asset_version_id)
    if v is None:
        return "failed", None, "asset version missing"
    asset_type = assets.asset_type_of(db, v)
    row = assets.specialised(db, v)
    if row is None:
        return "failed", None, "specialised row missing"
    if asset_type == "document":
        blob = db.get(Blob, row.blob_sha256)
        ctype = (blob.content_type if blob else "").lower()
        name = (row.original_name or "").lower()
        if not (ctype.startswith("text/") or ctype in TEXT_CONTENT_TYPES or name.endswith(TEXT_EXTENSIONS)):
            return "not_applicable", None, None
        data, _ = storage.read_blob(db, row.blob_sha256)
        text = data[: MAX_TEXT_CHARS * 4].decode("utf-8", errors="replace").replace("\x00", "")[:MAX_TEXT_CHARS]
        return "ready", text, None
    asset = db.get(Asset, v.asset_id)
    payload = {"asset_type": asset_type, "name": asset.name if asset else None, "label": assets.human_label(db, asset_type, row, v),
               "summary": assets._summary(db, asset_type, row)}
    return "ready", json.dumps(payload, ensure_ascii=False, default=str)[:MAX_TEXT_CHARS], None


def run_index(db: Session, collection_id: uuid.UUID) -> KnowledgeCollection | None:
    """Index the current manifest's items. Per-item failures are isolated; the collection ends
    ready / partial / failed. Never touches the manifest, hashes, versions or acceptance."""
    features.require_enabled("collaboration")
    col = db.scalar(select(KnowledgeCollection).where(KnowledgeCollection.id == collection_id).with_for_update())
    if col is None:
        log.warning("knowledge collection %s vanished before indexing", collection_id)
        return None
    col.index_status = "indexing"
    db.flush()
    items = list(db.scalars(select(KnowledgeItem).where(KnowledgeItem.collection_version_id == col.current_version_id)
                            .order_by(KnowledgeItem.created_at, KnowledgeItem.id)).all())
    ok = failed = 0
    for item in items:
        item_id = item.id
        try:
            with db.begin_nested():
                status, text, error = _extract_text(db, item)
                item.index_status, item.extracted_text, item.index_error = status, text, error
            if status == "failed":
                failed += 1
            else:
                ok += 1
        except Exception as exc:  # noqa: BLE001 - one item never blocks the others
            log.exception("knowledge item %s could not be indexed", item_id)
            db.execute(update(KnowledgeItem).where(KnowledgeItem.id == item_id)
                       .values(index_status="failed", extracted_text=None, index_error=str(exc)[:2000]))
            failed += 1
    if failed == 0:
        col.index_status, col.index_error = "ready", None
    elif ok == 0:
        col.index_status, col.index_error = "failed", f"{failed} item(s) could not be indexed"
    else:
        col.index_status, col.index_error = "partial", f"{failed} of {ok + failed} item(s) could not be indexed"
    db.flush()
    return col


def record_index_failure(db: Session, collection_id: uuid.UUID, error: str) -> None:
    col = db.get(KnowledgeCollection, collection_id)
    if col is not None:
        col.index_status = "failed"
        col.index_error = error[:2000]


def reindex(db: Session, col: KnowledgeCollection, *, actor: ActorRef) -> KnowledgeCollection:
    features.require_enabled("collaboration")
    queue_index(db, col, reason="reindex")
    audit(db, action="knowledge.reindex", subject_type="knowledge_collection", subject_id=col.id, user_id=actor.user_id, project_id=col.project_id,
          data={"version_id": _s(col.current_version_id)})
    db.flush()
    return col


# --- invalidation and reuse ---------------------------------------------------------------------


def invalidate_items(db: Session, node_task: CollaborationNodeTask) -> int:
    """The source node was reopened: its archived items are no longer reusable by default
    (references stay; new adoptions are refused with item_not_reusable)."""
    res = db.execute(update(KnowledgeItem).where(KnowledgeItem.node_task_id == node_task.id, KnowledgeItem.reusable.is_(True)).values(reusable=False))
    return int(res.rowcount or 0)


def available_items(db: Session, project_id: uuid.UUID, *, agent_scope: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Items an agent may reference or adopt: reusable items of the active collections' current
    manifests in this project (knowledge_scope {"scope": "none"} → nothing)."""
    if (agent_scope or {}).get("scope", "project") == "none":
        return []
    rows = db.execute(
        select(KnowledgeItem, KnowledgeCollection, AssetVersion, Asset)
        .join(KnowledgeCollection, KnowledgeCollection.current_version_id == KnowledgeItem.collection_version_id)
        .join(AssetVersion, AssetVersion.id == KnowledgeItem.asset_version_id)
        .join(Asset, Asset.id == AssetVersion.asset_id)
        .where(KnowledgeCollection.project_id == project_id, KnowledgeCollection.status == "active", KnowledgeItem.reusable.is_(True))
        .order_by(KnowledgeCollection.created_at.desc(), KnowledgeItem.created_at, KnowledgeItem.id)
    ).all()
    out = []
    for item, col, v, asset in rows:
        out.append({
            "id": str(item.id), "collection_id": str(col.id), "collection_title": col.title, "collection_version_id": str(item.collection_version_id),
            "asset_version_id": str(v.id), "asset_type": asset.type, "asset_name": asset.name, "version_no": v.version_no,
            "content_hash": item.content_hash, "kind": item.kind, "slot_key": item.slot_key, "run_id": _s(item.run_id),
            "task_id": _s(item.task_id), "node_task_id": _s(item.node_task_id), "index_status": item.index_status,
            "excerpt": (item.extracted_text or "")[:2000],
        })
    return out


def _resolve_target(db: Session, target: dict[str, Any]) -> tuple[Task, CollaborationNodeTask | None, WorkflowRun | None, CollaborationRun | None]:
    target = target or {}
    task_id, run_id, node_id = target.get("task_id"), target.get("run_id"), target.get("node_id")
    nt: CollaborationNodeTask | None = None
    if run_id and node_id:
        nt = db.scalar(select(CollaborationNodeTask).where(CollaborationNodeTask.run_id == run_id, CollaborationNodeTask.node_id == node_id))
        if nt is None:
            raise not_found("node task")
        task = db.get(Task, nt.task_id)
    elif task_id:
        task = db.get(Task, task_id)
        if task is not None:
            nt = db.scalar(select(CollaborationNodeTask).where(CollaborationNodeTask.task_id == task.id))
    else:
        raise AppError(422, "target_required", "give the target task_id, or run_id and node_id")
    if task is None:
        raise not_found("task")
    run = db.get(WorkflowRun, nt.run_id) if nt else None
    crun = db.get(CollaborationRun, nt.run_id) if nt else None
    return task, nt, run, crun


def _adopt(db: Session, *, task: Task, slot_key: str, version_id: uuid.UUID, reason: str, actor: ActorRef,
           on_behalf_user_id: uuid.UUID) -> tuple[TaskBaselineSnapshot, bool]:
    """Adopt one knowledge version as the task's baseline slot through tasks.adopt_baseline;
    returns (snapshot, created). The caller has already established who may adopt (the
    designated human reviewer, or the executing agent in a full-auto instance), so the access
    object carries the owner role and the call runs in the collaboration scope (the generic
    baseline endpoint refuses collaboration nodes); an agent actor is written back onto the
    snapshot (adopted_by_kind=agent)."""
    from . import agent_runtime
    from . import tasks as task_svc

    project = db.get(Project, task.project_id)
    user = db.get(User, on_behalf_user_id)
    if project is None or user is None:
        raise not_found("task")
    acc = ProjectAccess(project=project, user=user, role="owner")
    with agent_runtime.collaboration_scope():
        snap, _notices, created = task_svc.adopt_baseline(db, acc, task.id, items={slot_key: version_id}, reason=reason)
    if created and actor.is_agent:
        actors.assign_legacy(snap, "adopted_by", actor)
        db.flush()
    return snap, created


def _requeue_after_baseline(db: Session, nt: CollaborationNodeTask) -> bool:
    """A confirmed adoption changed the node's baseline: the same supersede / re-queue step as
    C04 adopt_baseline (agent_runtime._task_action) — a running execute attempt of the node is
    superseded and an agent executor is queued again on the new baseline. Caller holds the
    task lock (project → task → run → node task, as in C04). Returns True when an attempt
    was superseded."""
    from ..workflow import engine
    from . import agent_runtime as rt

    engine.lock_run(db, nt.run_id)
    nt = rt.lock_node_task(db, nt.id)
    requeued = False
    for ex in db.scalars(select(AgentExecution).where(AgentExecution.node_task_id == nt.id, AgentExecution.status == "running",
                                                       AgentExecution.role == "execute")).all():
        ex.status = "superseded"
        ex.error = {"code": "baseline_changed", "message": "the baseline changed; the attempt is re-queued on the new baseline"}
        ex.finished_at = _now()
        requeued = True
    if nt.executor_kind == "agent" and nt.phase in ("executing", "ready", "rework"):
        nt.phase = "rework" if int(rt.budget_of(nt.budget)["reworks"]) > 0 else "ready"
        rt._clear_lease(nt)
    rt._bump(nt)
    return requeued


def _adoptable(db: Session, item: KnowledgeItem | None, *, lock: bool) -> tuple[KnowledgeItem, KnowledgeCollectionVersion, KnowledgeCollection] | None:
    """(item, version, collection) when the item may be adopted now: the collection is active,
    the item belongs to its current manifest version and is still reusable. `lock` takes the
    collection row FOR SHARE, so a concurrent deprecation waits (or is seen) before the check."""
    if item is None:
        return None
    ver = db.get(KnowledgeCollectionVersion, item.collection_version_id)
    if ver is None:
        return None
    stmt = select(KnowledgeCollection).where(KnowledgeCollection.id == ver.collection_id)
    if lock:
        stmt = stmt.with_for_update(read=True).execution_options(populate_existing=True)
    col = db.scalar(stmt)
    if col is None:
        return None
    item = db.scalar(select(KnowledgeItem).where(KnowledgeItem.id == item.id).execution_options(populate_existing=True))
    if item is None or not item.reusable or col.status != "active" or col.current_version_id != ver.id:
        return None
    return item, ver, col


def use_item(
    db: Session,
    *,
    item_id: uuid.UUID,
    usage: str,
    target: dict[str, Any],
    actor: ActorRef,
    reason: str,
    slot_key: str | None = None,
    mode: str | None = None,
) -> KnowledgeUsage:
    """Reference or adopt an archived item (§4 C09). `mode` defaults to the target instance's
    execution mode (a plain task counts as semi_auto). Caller checks project access and commits.

    reference: a usage record only. baseline: semi_auto → confirmation pending (the node's human
    reviewer confirms, which adopts the baseline); full_auto → only the executing agent adopts,
    directly. A non-reusable item cannot be adopted (409 item_not_reusable)."""
    features.require_enabled("collaboration")
    if usage not in KNOWLEDGE_USAGES:
        raise AppError(422, "unknown_usage", "usage is reference or baseline", {"usage": usage})
    reason = (reason or "").strip()
    if not reason:
        raise AppError(422, "reason_required", "give the reason for using this knowledge item")
    item = db.get(KnowledgeItem, item_id)
    if item is None:
        raise not_found("knowledge item")
    ver = db.get(KnowledgeCollectionVersion, item.collection_version_id)
    col = db.get(KnowledgeCollection, ver.collection_id) if ver else None
    if ver is None or col is None:
        raise not_found("knowledge item")
    task, nt, run, crun = _resolve_target(db, target)
    if actor.is_agent:
        actors.profile_for(db, task.project_id, actor.agent_profile_id)  # an agent of the target project only (404 otherwise)
    effective_mode = mode or (crun.execution_mode if crun else "semi_auto")
    if usage == "baseline":
        if not slot_key:
            raise AppError(422, "slot_required", "a baseline adoption needs the target slot_key")
        if not re.match(SLOT_RE, slot_key):
            raise AppError(422, "invalid_slot", "slot names are lower-case identifiers such as target_model", {"slot": slot_key})
        if _adoptable(db, item, lock=True) is None:
            raise conflict("item_not_reusable", "this item is not reusable by default (failed, superseded, historical or deprecated); referencing is still allowed",
                           item_id=str(item.id), reusable=item.reusable, collection_status=col.status)
        if effective_mode == "full_auto" and not actor.is_agent:
            raise conflict("mode_forbids", "a full-auto instance only adopts knowledge through its executing agent; fork it to semi-auto first")
        if effective_mode == "full_auto" and nt is not None and nt.executor_agent_id != actor.agent_profile_id:
            raise AppError(403, "actor_not_allowed", "only the node's executing agent adopts knowledge in a full-auto instance",
                           {"executor_agent_id": _s(nt.executor_agent_id)})
    u = KnowledgeUsage(
        item_id=item.id, collection_version_id=ver.id, asset_version_id=item.asset_version_id, content_hash=item.content_hash,
        target_project_id=task.project_id, target_run_id=run.id if run else None, target_node_task_id=nt.id if nt else None,
        target_task_id=task.id, slot_key=slot_key, usage=usage, reason=reason, confirmation="none",
    )
    actors.assign(u, "used_by", actor)
    db.add(u)
    db.flush()
    if usage == "baseline":
        if effective_mode == "full_auto":
            on_behalf = run.created_by if run else task.created_by
            # The executing agent adopts during its own attempt: nothing to re-queue.
            snap, _created = _adopt(db, task=task, slot_key=slot_key, version_id=item.asset_version_id, reason=f"knowledge adoption: {reason}", actor=actor,
                                    on_behalf_user_id=on_behalf)
            u.baseline_snapshot_id = snap.id
            u.confirmation = "confirmed"
            u.confirmed_at = _now()
        else:
            u.confirmation = "pending"
    audit(db, action=f"knowledge.use.{usage}", subject_type="knowledge_usage", subject_id=u.id, user_id=actor.user_id, project_id=task.project_id,
          data={"item_id": str(item.id), "collection_id": str(col.id), "target_task_id": str(task.id), "slot_key": slot_key, "mode": effective_mode,
                "confirmation": u.confirmation, "actor": actor.model_dump(mode="json"), "reason": reason})
    db.flush()
    return u


def confirm_usage(db: Session, *, usage_id: uuid.UUID, user: User, decision: str, comment: str = "") -> KnowledgeUsage:
    """The target node's designated human reviewer confirms or rejects a pending baseline
    adoption; confirming adopts the baseline (adopted_by = the confirming person). The new
    task still needs its own acceptance.

    The usage row is locked and its state re-checked under the lock, so a double confirm
    adopts once (the second gets 409 usage_closed). Confirming needs the item to be adoptable
    now (active collection, current manifest version, still reusable; else 409
    item_not_reusable); a running agent attempt of the target node is superseded and the
    node re-queued, as with C04 adopt_baseline."""
    if decision not in ("confirmed", "rejected"):
        raise AppError(422, "unknown_decision", "decision is confirmed or rejected")
    u = db.get(KnowledgeUsage, usage_id)
    if u is None:
        raise not_found("knowledge usage")
    project_access(db, user, u.target_project_id)
    if u.usage != "baseline":
        raise conflict("usage_not_baseline", "only baseline adoptions are confirmed")
    u = db.scalar(select(KnowledgeUsage).where(KnowledgeUsage.id == usage_id).with_for_update().execution_options(populate_existing=True))
    if u is None:
        raise not_found("knowledge usage")
    if u.confirmation != "pending":
        raise conflict("usage_closed", f"this adoption is already {u.confirmation}", confirmation=u.confirmation)
    task = db.get(Task, u.target_task_id) if u.target_task_id else None
    nt = db.get(CollaborationNodeTask, u.target_node_task_id) if u.target_node_task_id else None
    if task is None:
        raise conflict("target_missing", "the target task no longer exists")
    reviewer_id = nt.reviewer_user_id if nt is not None and nt.reviewer_kind == "human" else task.reviewer_id
    if reviewer_id is None or reviewer_id != user.id:
        raise AppError(403, "not_reviewer", "only the node's designated human reviewer can confirm a knowledge adoption",
                       {"reviewer_user_id": _s(reviewer_id)})
    features.require_enabled("collaboration")
    requeued = False
    if decision == "confirmed":
        found = _adoptable(db, db.get(KnowledgeItem, u.item_id), lock=True)
        if found is None:
            raise conflict("item_not_reusable", "the item is no longer reusable (deprecated, superseded or not in the current manifest); reject this adoption",
                           item_id=str(u.item_id))
        snap, created = _adopt(db, task=task, slot_key=u.slot_key or "", version_id=u.asset_version_id, reason=f"knowledge adoption confirmed: {u.reason}",
                               actor=actors.of_user(user), on_behalf_user_id=user.id)
        u.baseline_snapshot_id = snap.id
        if created and nt is not None:
            requeued = _requeue_after_baseline(db, nt)
    u.confirmation = decision
    u.confirmed_by_user_id = user.id
    u.confirmed_at = _now()
    audit(db, action=f"knowledge.usage.{decision}", subject_type="knowledge_usage", subject_id=u.id, user_id=user.id, project_id=u.target_project_id,
          data={"comment": (comment or "")[:4000], "baseline_snapshot_id": _s(u.baseline_snapshot_id), "target_task_id": str(task.id),
                "requeued": requeued})
    db.flush()
    return u


# --- views --------------------------------------------------------------------------------------


def _member_projects(db: Session, user: User) -> set[uuid.UUID]:
    return set(db.scalars(select(ProjectMember.project_id).where(ProjectMember.user_id == user.id)).all())


def usage_out(db: Session, u: KnowledgeUsage, *, collection_id: uuid.UUID | None = None) -> dict[str, Any]:
    return {
        "id": str(u.id),
        "item_id": str(u.item_id),
        "collection_id": _s(collection_id),
        "collection_version_id": str(u.collection_version_id),
        "asset_version_id": str(u.asset_version_id),
        "content_hash": u.content_hash,
        "usage": u.usage,
        "target_project_id": str(u.target_project_id),
        "target_run_id": _s(u.target_run_id),
        "target_node_task_id": _s(u.target_node_task_id),
        "target_task_id": _s(u.target_task_id),
        "slot_key": u.slot_key,
        "baseline_snapshot_id": _s(u.baseline_snapshot_id),
        "used_by": actors.out(db, u.used_by_kind, u.used_by_user_id, u.used_by_agent_id),
        "reason": u.reason,
        "confirmation": u.confirmation,
        "confirmed_by_user_id": _s(u.confirmed_by_user_id),
        "confirmed_at": _iso(u.confirmed_at),
        "created_at": _iso(u.created_at),
    }


def usages_out(db: Session, usages: list[KnowledgeUsage]) -> list[dict[str, Any]]:
    if not usages:
        return []
    ver_ids = {u.collection_version_id for u in usages}
    col_of = dict(db.execute(select(KnowledgeCollectionVersion.id, KnowledgeCollectionVersion.collection_id)
                             .where(KnowledgeCollectionVersion.id.in_(list(ver_ids)))).all())
    user_ids = {u.used_by_user_id for u in usages if u.used_by_user_id}
    agent_ids = {u.used_by_agent_id for u in usages if u.used_by_agent_id}
    # Strong references keep the actors in the (weak) identity map while the rows are rendered.
    held = list(db.scalars(select(User).where(User.id.in_(list(user_ids)))).all()) if user_ids else []
    held += list(db.scalars(select(AgentProfile).where(AgentProfile.id.in_(list(agent_ids)))).all()) if agent_ids else []
    out = [usage_out(db, u, collection_id=col_of.get(u.collection_version_id)) for u in usages]
    del held
    return out


def items_out(db: Session, items: list[KnowledgeItem]) -> list[dict[str, Any]]:
    """item_out per §4 C09: the version brief and acceptance come from the resource projection (batched)."""
    if not items:
        return []
    by_resource = collaboration_resources.items_for_resources(db, [i.resource_id for i in items if i.resource_id])
    fallback_ids = [i.asset_version_id for i in items if not i.resource_id or i.resource_id not in by_resource]
    fallback: dict[uuid.UUID, dict] = {}
    if fallback_ids:
        rows = db.execute(select(AssetVersion, Asset).join(Asset, Asset.id == AssetVersion.asset_id).where(AssetVersion.id.in_(fallback_ids))).all()
        labels = collaboration_resources.version_labels(db, [(v, a.type) for v, a in rows])
        fallback = {v.id: collaboration_resources.version_brief(v, a, label=labels[v.id], labels=list(v.labels or [])) for v, a in rows}
    out = []
    for i in items:
        res = by_resource.get(i.resource_id) if i.resource_id else None
        out.append({
            "id": str(i.id),
            "collection_version_id": str(i.collection_version_id),
            "resource_id": _s(i.resource_id),
            "asset_version_id": str(i.asset_version_id),
            "version": res["asset"] if res else fallback.get(i.asset_version_id, {"id": str(i.asset_version_id), "missing": True}),
            "content_hash": i.content_hash,
            "run_id": _s(i.run_id),
            "node_task_id": _s(i.node_task_id),
            "node_id": res["node_id"] if res else None,
            "task_id": _s(i.task_id),
            "task_key": res["task_key"] if res else None,
            "task_title": res["task_title"] if res else None,
            "slot_key": i.slot_key,
            "kind": i.kind,
            "resource_kind": res["kind"] if res else None,
            "reusable": i.reusable,
            "index_status": i.index_status,
            "index_error": i.index_error,
            "has_text": bool(i.extracted_text),
            "acceptance": res["task_acceptance"] if res else collaboration_resources._empty_acceptance("not_applicable"),
            "created_at": _iso(i.created_at),
        })
    return out


def _version_counts(db: Session, version_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, int]]:
    out: dict[uuid.UUID, dict[str, int]] = {vid: {"formal": 0, "history": 0, "candidate": 0, "failed": 0, "evidence": 0, "reusable": 0} for vid in version_ids}
    if not version_ids:
        return out
    stmt = (select(KnowledgeItem.collection_version_id, KnowledgeItem.kind, KnowledgeItem.reusable, func.count())
            .where(KnowledgeItem.collection_version_id.in_(version_ids)).group_by(KnowledgeItem.collection_version_id, KnowledgeItem.kind, KnowledgeItem.reusable))
    for vid, kind, reusable, n in db.execute(stmt).all():
        out[vid][kind] = out[vid].get(kind, 0) + int(n)
        if reusable:
            out[vid]["reusable"] += int(n)
    return out


def version_brief(ver: KnowledgeCollectionVersion, counts: dict[str, int]) -> dict[str, Any]:
    return {"id": str(ver.id), "collection_id": str(ver.collection_id), "version_no": ver.version_no, "manifest_hash": ver.manifest_hash,
            "archived_at": _iso(ver.archived_at), "counts": counts}


def collections_out(db: Session, cols: list[KnowledgeCollection]) -> list[dict[str, Any]]:
    if not cols:
        return []
    ids = [c.id for c in cols]
    run_ids = [c.run_id for c in cols]
    runs = {r.id: r for r in db.scalars(select(WorkflowRun).where(WorkflowRun.id.in_(run_ids))).all()}
    cur_ids = [c.current_version_id for c in cols if c.current_version_id]
    versions = {v.id: v for v in db.scalars(select(KnowledgeCollectionVersion).where(KnowledgeCollectionVersion.id.in_(cur_ids))).all()} if cur_ids else {}
    counts = _version_counts(db, cur_ids)
    versions_count = dict(db.execute(select(KnowledgeCollectionVersion.collection_id, func.count())
                                     .where(KnowledgeCollectionVersion.collection_id.in_(ids)).group_by(KnowledgeCollectionVersion.collection_id)).all())
    usage_counts: dict[uuid.UUID, dict[str, int]] = {cid: {"reference": 0, "baseline": 0} for cid in ids}
    for cid, usage, n in db.execute(
        select(KnowledgeCollectionVersion.collection_id, KnowledgeUsage.usage, func.count())
        .join(KnowledgeUsage, KnowledgeUsage.collection_version_id == KnowledgeCollectionVersion.id)
        .where(KnowledgeCollectionVersion.collection_id.in_(ids)).group_by(KnowledgeCollectionVersion.collection_id, KnowledgeUsage.usage)
    ).all():
        usage_counts[cid][usage] = int(n)
    out = []
    for c in cols:
        run = runs.get(c.run_id)
        cur = versions.get(c.current_version_id) if c.current_version_id else None
        out.append({
            "id": str(c.id),
            "project_id": str(c.project_id),
            "run_id": str(c.run_id),
            "run_title": run.title if run else None,
            "run_status": run.status if run else None,
            "title": c.title,
            "scope": c.scope or {},
            "status": c.status,
            "current_version": version_brief(cur, counts.get(cur.id, {})) if cur else None,
            "index_status": c.index_status,
            "index_error": c.index_error,
            "versions_count": int(versions_count.get(c.id, 0)),
            "usage_counts": usage_counts[c.id],
            "version": c.version,
            "created_by": actors.out(db, c.created_by_kind, c.created_by, c.created_by_agent_id),
            "created_at": _iso(c.created_at),
            "updated_at": _iso(c.updated_at),
            "deprecated_at": _iso(c.deprecated_at),
        })
    return out


def collection_out(db: Session, col: KnowledgeCollection) -> dict[str, Any]:
    return collections_out(db, [col])[0]


def list_collections(db: Session, user: User, project_id: uuid.UUID, *, q: str | None = None, reusable: bool | None = None,
                     status: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """C09 list: structured search only (ILIKE on title / scope / item asset names / extracted text)."""
    project_access(db, user, project_id)
    conds = [KnowledgeCollection.project_id == project_id]
    if status:
        conds.append(KnowledgeCollection.status == status)
    reusable_exists = exists().where(KnowledgeItem.collection_version_id == KnowledgeCollection.current_version_id, KnowledgeItem.reusable.is_(True))
    if reusable is True:
        conds.append(reusable_exists)
    elif reusable is False:
        conds.append(~reusable_exists)
    q = (q or "").strip()
    if q:
        like = collaboration_resources._like(q)
        item_match = (
            exists()
            .where(
                KnowledgeItem.collection_version_id == KnowledgeCollection.current_version_id,
                AssetVersion.id == KnowledgeItem.asset_version_id,
                Asset.id == AssetVersion.asset_id,
                or_(Asset.name.ilike(like, escape="\\"), KnowledgeItem.extracted_text.ilike(like, escape="\\")),
            )
        )
        conds.append(or_(KnowledgeCollection.title.ilike(like, escape="\\"), cast(KnowledgeCollection.scope, Text).ilike(like, escape="\\"), item_match))
    total = int(db.scalar(select(func.count()).select_from(KnowledgeCollection).where(*conds)) or 0)
    rows = list(db.scalars(select(KnowledgeCollection).where(*conds).order_by(KnowledgeCollection.created_at.desc(), KnowledgeCollection.id.desc())
                           .limit(limit).offset(offset)).all())
    return {"items": collections_out(db, rows), "total": total, "limit": limit, "offset": offset}


def load_collection(db: Session, user: User, collection_id: uuid.UUID, action: str = "view") -> tuple[KnowledgeCollection, ProjectAccess]:
    col = db.get(KnowledgeCollection, collection_id)
    if col is None:
        raise not_found("knowledge collection")
    acc = project_access(db, user, col.project_id, action)
    return col, acc


def collection_detail(db: Session, user: User, col: KnowledgeCollection, *, version_id: uuid.UUID | None = None) -> dict[str, Any]:
    ver = db.get(KnowledgeCollectionVersion, version_id or col.current_version_id) if (version_id or col.current_version_id) else None
    if version_id is not None and (ver is None or ver.collection_id != col.id):
        raise not_found("knowledge version")
    data = collection_out(db, col)
    all_versions = list(db.scalars(select(KnowledgeCollectionVersion).where(KnowledgeCollectionVersion.collection_id == col.id)
                                   .order_by(KnowledgeCollectionVersion.version_no.desc())).all())
    counts = _version_counts(db, [v.id for v in all_versions])
    data["versions"] = [{**version_brief(v, counts.get(v.id, {})), "current": v.id == col.current_version_id} for v in all_versions]
    if ver is None:
        data.update({"version": None, "items": [], "usages": []})
        return data
    items = list(db.scalars(select(KnowledgeItem).where(KnowledgeItem.collection_version_id == ver.id).order_by(KnowledgeItem.created_at, KnowledgeItem.id)).all())
    visible = _member_projects(db, user)
    usages = [u for u in db.scalars(
        select(KnowledgeUsage).join(KnowledgeCollectionVersion, KnowledgeCollectionVersion.id == KnowledgeUsage.collection_version_id)
        .where(KnowledgeCollectionVersion.collection_id == col.id).order_by(KnowledgeUsage.created_at.desc(), KnowledgeUsage.id.desc())
    ).all() if u.target_project_id in visible]
    data["version"] = {**version_brief(ver, counts.get(ver.id, {})), "current": ver.id == col.current_version_id, "manifest": ver.manifest or {}}
    data["items"] = items_out(db, items)
    data["usages"] = usages_out(db, usages)
    return data


def _recompute_reusable(db: Session, col: KnowledgeCollection) -> int:
    """Default reuse of the current manifest's items by the archive rule: a formal item is
    reusable while its formal output is still valid (no superseded row for it, its node still
    accepts that submission); every other item and every older manifest stays not reusable.
    Returns the number of reusable items."""
    if col.current_version_id is None:
        return 0
    sup = aliased(WorkflowResource, name="sup")
    valid = list(db.scalars(
        select(KnowledgeItem.id)
        .join(WorkflowResource, WorkflowResource.id == KnowledgeItem.resource_id)
        .outerjoin(CollaborationNodeTask, CollaborationNodeTask.id == WorkflowResource.node_task_id)
        .where(
            KnowledgeItem.collection_version_id == col.current_version_id,
            KnowledgeItem.kind == "formal",
            ~exists().where(sup.kind == "superseded", sup.node_task_id == WorkflowResource.node_task_id,
                            sup.slot_key.is_not_distinct_from(WorkflowResource.slot_key), sup.asset_version_id == WorkflowResource.asset_version_id,
                            sup.submission_id.is_not_distinct_from(WorkflowResource.submission_id)),
            or_(CollaborationNodeTask.id.is_(None), CollaborationNodeTask.accepted_submission_id.is_not_distinct_from(WorkflowResource.submission_id)),
        )
    ).all())
    db.execute(update(KnowledgeItem).where(KnowledgeItem.collection_version_id == col.current_version_id, KnowledgeItem.id.not_in(valid))
               .values(reusable=False), execution_options={"synchronize_session": False})
    if valid:
        db.execute(update(KnowledgeItem).where(KnowledgeItem.id.in_(valid)).values(reusable=True), execution_options={"synchronize_session": False})
    return len(valid)


def patch_collection(db: Session, acc: ProjectAccess, col: KnowledgeCollection, *, expected_version: int, title: str | None = None,
                     scope: dict[str, Any] | None = None, status: str | None = None) -> KnowledgeCollection:
    """C09 PATCH under the collection row lock. Deprecating takes every item of the collection
    out of default reuse (references stay allowed); restoring `active` recomputes the current
    manifest's reusable items from the archive rule (reopened sources stay out)."""
    features.require_enabled("collaboration")
    col = db.scalar(select(KnowledgeCollection).where(KnowledgeCollection.id == col.id).with_for_update().execution_options(populate_existing=True))
    if col is None:
        raise not_found("knowledge collection")
    if col.version != expected_version:
        raise conflict("version_conflict", "the collection was changed by someone else; reload first", current_version=col.version)
    changes: dict[str, Any] = {}
    if title is not None:
        title = title.strip()
        if not title:
            raise AppError(422, "title_required", "the title cannot be empty")
        changes["title"] = {"from": col.title, "to": title[:300]}
        col.title = title[:300]
    if scope is not None:
        changes["scope"] = {"from": col.scope, "to": scope}
        col.scope = scope
    if status is not None and status != col.status:
        changes["status"] = {"from": col.status, "to": status}
        col.status = status
        col.deprecated_at = _now() if status == "deprecated" else None
        db.flush()
        if status == "deprecated":
            versions = select(KnowledgeCollectionVersion.id).where(KnowledgeCollectionVersion.collection_id == col.id)
            res = db.execute(update(KnowledgeItem).where(KnowledgeItem.collection_version_id.in_(versions), KnowledgeItem.reusable.is_(True))
                             .values(reusable=False), execution_options={"synchronize_session": False})
            changes["reusable_items"] = {"withdrawn": int(res.rowcount or 0)}
        else:
            changes["reusable_items"] = {"reusable": _recompute_reusable(db, col)}
    col.version += 1
    audit(db, action="knowledge.update", subject_type="knowledge_collection", subject_id=col.id, user_id=acc.user.id, project_id=col.project_id, data=changes)
    db.flush()
    return col
