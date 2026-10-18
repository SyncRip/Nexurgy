"""Workflow resources: uses of exact asset versions in a collaboration run
(design §2.3, §3.10, §4 C08 / C10). Backend E owns this module.

A resource row is one *use relationship* (input / candidate / formal / failed /
superseded / evidence) of one exact version in one run, node and slot. Rows are
immutable: when a formal output loses its status a `superseded` row is added
next to it (`supersede_formal`). The query side batches every lookup per page
(no per-row SQL) and derives the read-only `task_acceptance` projection from the
frozen submission, its task_acceptance request and the latest decision.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import String, and_, case, cast, exists, func, literal, or_, select, union_all
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, aliased

from ..errors import not_found
from ..models import (
    RESOURCE_KINDS,
    AgentProfile,
    ApprovalDecision,
    ApprovalRequest,
    Asset,
    AssetVersion,
    CollaborationNodeTask,
    CollaborationRun,
    Drawing,
    FileVersion,
    Intake,
    ModelRevision,
    NodeRun,
    ProcessPlan,
    Quote,
    SubmissionItem,
    Task,
    TaskSubmission,
    User,
    WorkflowResource,
    WorkflowRun,
)
from ..security import project_access
from . import actors
from .assets import LABEL_PREFIX

KINDS: tuple[str, ...] = RESOURCE_KINDS
ACCEPTANCE_STATUSES = ("not_applicable", "unsubmitted", "in_review", "approved", "rejected", "invalidated")
SUBMISSION_STATUS_MAP = {"pending": "in_review", "approved": "approved", "rejected": "rejected", "withdrawn": "unsubmitted"}
MAX_LIMIT = 100


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _s(value: Any) -> str | None:
    return str(value) if value is not None else None


# --- registration (called by the runtime, backend D) -----------------------------------


def _dedupe_key(run_id: uuid.UUID, kind: str, version_id: uuid.UUID, node_task_id: uuid.UUID | None, slot_key: str | None,
                submission_id: uuid.UUID | None) -> str:
    return f"{run_id}:{kind}:{version_id}:{node_task_id or '-'}:{slot_key or '-'}:{submission_id or '-'}"[:200]


def register(
    db: Session,
    *,
    run: WorkflowRun,
    kind: str,
    asset_version: AssetVersion,
    node_task: CollaborationNodeTask | None = None,
    node_run_id: uuid.UUID | None = None,
    agent_execution_id: uuid.UUID | None = None,
    submission_id: uuid.UUID | None = None,
    attempt: int | None = None,
    slot_key: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> WorkflowResource:
    """Record one use relationship (idempotent per dedupe_key). Caller owns the transaction."""
    from . import feature_flags as features

    features.require_run_continuation(db, run.id)
    if node_task is not None and node_task.run_id != run.id:
        raise ValueError("the resource node task belongs to another run")
    if kind not in KINDS:
        raise ValueError(f"unknown resource kind {kind}")
    key = _dedupe_key(run.id, kind, asset_version.id, node_task.id if node_task else None, slot_key, submission_id)
    db.execute(
        pg_insert(WorkflowResource)
        .values(
            id=uuid.uuid4(), run_id=run.id, project_id=run.project_id, node_task_id=node_task.id if node_task else None,
            node_run_id=node_run_id, agent_execution_id=agent_execution_id, submission_id=submission_id, attempt=attempt,
            slot_key=slot_key, asset_version_id=asset_version.id, content_hash=asset_version.content_hash, kind=kind,
            metadata=metadata or {}, dedupe_key=key,
        )
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
    )
    row = db.scalar(select(WorkflowResource).where(WorkflowResource.dedupe_key == key))
    assert row is not None
    return row


def supersede_formal(db: Session, *, node_task: CollaborationNodeTask, reason: str) -> list[WorkflowResource]:
    """Add a `superseded` row for every current formal output of the node (history is kept)."""
    run = db.get(WorkflowRun, node_task.run_id)
    assert run is not None
    out: list[WorkflowResource] = []
    for f in db.scalars(select(WorkflowResource).where(WorkflowResource.node_task_id == node_task.id, WorkflowResource.kind == "formal")).all():
        v = db.get(AssetVersion, f.asset_version_id)
        if v is None:
            continue
        out.append(register(db, run=run, kind="superseded", asset_version=v, node_task=node_task, slot_key=f.slot_key,
                            submission_id=f.submission_id, metadata={"reason": reason, "formal_resource_id": str(f.id), "phase": node_task.phase}))
    return out


# --- query core ---------------------------------------------------------------------------

_Producer = aliased(WorkflowRun, name="producer_run")
_Sup = aliased(WorkflowResource, name="superseding")
# A formal row loses its status when a superseded row exists for the same node task / slot / version
# and the same accepting submission (supersede_formal copies it), so re-accepting the same version
# under a new submission yields a fresh, valid formal row.
_SUPERSEDED = (
    exists()
    .where(
        _Sup.kind == "superseded",
        _Sup.node_task_id == WorkflowResource.node_task_id,
        _Sup.slot_key.is_not_distinct_from(WorkflowResource.slot_key),
        _Sup.asset_version_id == WorkflowResource.asset_version_id,
        _Sup.submission_id.is_not_distinct_from(WorkflowResource.submission_id),
        _Sup.id != WorkflowResource.id,
    )
    .label("superseded")
)
# Version label as searched by `q` (R3 / D1 / P2 / Q1 / I1 / v2 / M1).
_LABEL_EXPR = func.concat(case(*[(Asset.type == t, p) for t, p in LABEL_PREFIX.items()], else_="v"), cast(AssetVersion.version_no, String))


def _joins(stmt):
    return (
        stmt.join(AssetVersion, AssetVersion.id == WorkflowResource.asset_version_id)
        .join(Asset, Asset.id == AssetVersion.asset_id)
        .join(WorkflowRun, WorkflowRun.id == WorkflowResource.run_id)
        .outerjoin(CollaborationRun, CollaborationRun.run_id == WorkflowResource.run_id)
        .outerjoin(CollaborationNodeTask, CollaborationNodeTask.id == WorkflowResource.node_task_id)
        .outerjoin(Task, Task.id == CollaborationNodeTask.task_id)
        .outerjoin(NodeRun, NodeRun.id == AssetVersion.node_run_id)
        .outerjoin(_Producer, _Producer.id == NodeRun.run_id)
    )


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _where(stmt, f: dict[str, Any], *, with_kind: bool = True):
    conds = []
    if f.get("project_id") is not None:
        conds.append(WorkflowResource.project_id == f["project_id"])
    if f.get("run_id") is not None:
        conds.append(WorkflowResource.run_id == f["run_id"])
    if f.get("node_id"):
        conds.append(CollaborationNodeTask.node_id == f["node_id"])
    if f.get("task_id") is not None:
        conds.append(CollaborationNodeTask.task_id == f["task_id"])
    if f.get("asset_version_id") is not None:
        conds.append(WorkflowResource.asset_version_id == f["asset_version_id"])
    if f.get("node_task_id") is not None:
        conds.append(WorkflowResource.node_task_id == f["node_task_id"])
    if with_kind and f.get("kind"):
        conds.append(WorkflowResource.kind == f["kind"])
    if f.get("asset_type"):
        conds.append(Asset.type == f["asset_type"])
    if f.get("availability"):
        conds.append(AssetVersion.availability == f["availability"])
    if f.get("publish_state"):
        conds.append(AssetVersion.publish_state == f["publish_state"])
    if f.get("task_status"):
        conds.append(Task.status == f["task_status"])
    if f.get("project_ids") is not None:
        conds.append(WorkflowResource.project_id.in_(list(f["project_ids"])))
    q = (f.get("q") or "").strip()
    if q:
        like = _like(q)
        conds.append(or_(
            Asset.name.ilike(like, escape="\\"),
            WorkflowResource.slot_key.ilike(like, escape="\\"),
            AssetVersion.content_hash.ilike(like, escape="\\"),
            _LABEL_EXPR.ilike(like, escape="\\"),
        ))
    return stmt.where(*conds) if conds else stmt


def _rows_select():
    return select(WorkflowResource, AssetVersion, Asset, WorkflowRun, CollaborationRun, CollaborationNodeTask, Task,
                  NodeRun.status, _Producer.status, _SUPERSEDED)


def _order(stmt):
    return stmt.order_by(WorkflowResource.created_at.desc(), WorkflowResource.id.desc())


def facets(db: Session, f: dict[str, Any]) -> dict[str, int]:
    """Count per kind under every filter except `kind` (six keys, zeros included)."""
    stmt = _where(_joins(select(WorkflowResource.kind, func.count()).select_from(WorkflowResource)), f, with_kind=False)
    by_kind = {k: 0 for k in KINDS}
    for kind, n in db.execute(stmt.group_by(WorkflowResource.kind)).all():
        by_kind[kind] = int(n)
    return by_kind


def page(db: Session, f: dict[str, Any], *, limit: int, offset: int) -> dict[str, Any]:
    """{items, total, limit, offset, facets} for one filter set. Six SQL statements at most:
    rows (with every joined fact), facets, labels, acceptance context, users, agents."""
    rows = db.execute(_order(_where(_joins(_rows_select()), f)).limit(limit).offset(offset)).all()
    by_kind = facets(db, f)
    total = by_kind.get(f["kind"], 0) if f.get("kind") else sum(by_kind.values())
    return {"items": items_from_rows(db, rows), "total": total, "limit": limit, "offset": offset, "facets": {"by_kind": by_kind}}


def rows_for(db: Session, f: dict[str, Any], *, limit: int | None = None, offset: int = 0) -> list[Any]:
    stmt = _order(_where(_joins(_rows_select()), f)).offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    return db.execute(stmt).all()


# --- batched labels -----------------------------------------------------------------------

_ORDINAL_MODELS: tuple[tuple[str, type], ...] = (("drawing", Drawing), ("process_plan", ProcessPlan), ("quote", Quote), ("intake", Intake))


def version_labels(db: Session, versions: list[tuple[AssetVersion, str]]) -> dict[uuid.UUID, str]:
    """Human labels (R3 / D1 / v2 …) for many versions in one statement (same rule as assets.human_label)."""
    out: dict[uuid.UUID, str] = {}
    parts = []
    by_target: dict[tuple[str, uuid.UUID], uuid.UUID] = {}
    rev_ids, fv_ids = [], []
    ordinal_ids: dict[str, list[uuid.UUID]] = {t: [] for t, _ in _ORDINAL_MODELS}
    for v, asset_type in versions:
        if asset_type == "process_module":
            out[v.id] = f"M{v.version_no}"
        elif asset_type == "part_model" and v.model_revision_id:
            rev_ids.append(v.model_revision_id)
            by_target[("part_model", v.model_revision_id)] = v.id
        elif asset_type == "document" and v.file_version_id:
            fv_ids.append(v.file_version_id)
            by_target[("document", v.file_version_id)] = v.id
        elif asset_type in ordinal_ids:
            target = getattr(v, {"drawing": "drawing_id", "process_plan": "process_plan_id", "quote": "quote_id", "intake": "intake_id"}[asset_type])
            if target:
                ordinal_ids[asset_type].append(target)
                by_target[(asset_type, target)] = v.id
        else:
            out[v.id] = f"{LABEL_PREFIX.get(asset_type, 'v')}{v.version_no}"
    if rev_ids:
        parts.append(select(literal("part_model", String).label("t"), ModelRevision.id.label("rid"), ModelRevision.rev_no.label("n"))
                     .where(ModelRevision.id.in_(rev_ids)))
    if fv_ids:
        parts.append(select(literal("document", String).label("t"), FileVersion.id.label("rid"), FileVersion.version.label("n"))
                     .where(FileVersion.id.in_(fv_ids)))
    for asset_type, model in _ORDINAL_MODELS:
        ids = ordinal_ids[asset_type]
        if not ids:
            continue
        part_ids = select(model.part_id).where(model.id.in_(ids)).scalar_subquery()
        numbered = (
            select(model.id.label("rid"), func.row_number().over(partition_by=model.part_id, order_by=(model.created_at, model.id)).label("n"))
            .where(model.part_id.in_(part_ids))
            .subquery()
        )
        parts.append(select(literal(asset_type, String).label("t"), numbered.c.rid, numbered.c.n).where(numbered.c.rid.in_(ids)))
    if parts:
        stmt = union_all(*parts) if len(parts) > 1 else parts[0]
        for t, rid, n in db.execute(stmt).all():
            vid = by_target.get((t, rid))
            if vid is not None:
                out[vid] = f"{LABEL_PREFIX[t]}{n}"
    for v, asset_type in versions:
        out.setdefault(v.id, f"{LABEL_PREFIX.get(asset_type, 'v')}{v.version_no}")
    return out


def version_brief(v: AssetVersion, asset: Asset, *, label: str, labels: list[str]) -> dict[str, Any]:
    return {
        "id": str(v.id),
        "asset_id": str(asset.id),
        "asset_type": asset.type,
        "asset_name": asset.name,
        "project_id": str(asset.project_id),
        "version_no": v.version_no,
        "label": label,
        "content_hash": v.content_hash,
        "availability": v.availability,
        "publish_state": v.publish_state,
        "labels": labels,
        "parent_version_id": _s(v.parent_version_id),
        "node_run_id": _s(v.node_run_id),
        "created_by": _s(v.created_by),
        "created_at": _iso(v.created_at),
    }


def _labels_of(v: AssetVersion, node_status: str | None, run_status: str | None) -> list[str]:
    """Same rule as ``assets.labels_of`` (a partial run counts as failed for its outputs)."""
    from .assets import FAILED_RUN_STATUSES

    labels = list(v.labels or [])
    if v.node_run_id and "from_failed_run" not in labels and (node_status in ("failed", "cancelled") or run_status in FAILED_RUN_STATUSES):
        labels.append("from_failed_run")
    return labels


# --- acceptance projection -----------------------------------------------------------------


class _AcceptanceContext:
    """Submissions, their task_acceptance requests and latest decisions for a set of tasks (one statement)."""

    def __init__(self, db: Session, task_ids: set[uuid.UUID]):
        self.db = db
        self.subs: dict[uuid.UUID, TaskSubmission] = {}
        self.sub_versions: dict[uuid.UUID, set[uuid.UUID]] = {}
        self.sub_hashes: dict[uuid.UUID, dict[uuid.UUID, str]] = {}
        self.by_task: dict[uuid.UUID, list[TaskSubmission]] = {}
        self.request: dict[uuid.UUID, ApprovalRequest] = {}
        self.decision: dict[uuid.UUID, ApprovalDecision] = {}
        self._actors: list[Any] = []
        if not task_ids:
            return
        stmt = (
            select(TaskSubmission, SubmissionItem, ApprovalRequest, ApprovalDecision)
            .outerjoin(SubmissionItem, SubmissionItem.submission_id == TaskSubmission.id)
            .outerjoin(ApprovalRequest, and_(ApprovalRequest.submission_id == TaskSubmission.id, ApprovalRequest.kind == "task_acceptance"))
            .outerjoin(ApprovalDecision, ApprovalDecision.request_id == ApprovalRequest.id)
            .where(TaskSubmission.task_id.in_(list(task_ids)))
        )
        for sub, item, req, dec in db.execute(stmt).all():
            if sub.id not in self.subs:
                self.subs[sub.id] = sub
                self.by_task.setdefault(sub.task_id, []).append(sub)
            if item is not None:
                self.sub_versions.setdefault(sub.id, set()).add(item.asset_version_id)
                self.sub_hashes.setdefault(sub.id, {})[item.asset_version_id] = item.content_hash
            if req is not None:
                cur = self.request.get(sub.id)
                if cur is None or (req.created_at, req.id) > (cur.created_at, cur.id):
                    self.request[sub.id] = req
            if dec is not None:
                cur_d = self.decision.get(dec.request_id)
                if cur_d is None or (dec.created_at, dec.id) > (cur_d.created_at, cur_d.id):
                    self.decision[dec.request_id] = dec
        user_ids = {d.actor_id for d in self.decision.values() if d.actor_id}
        agent_ids = {d.actor_agent_id for d in self.decision.values() if d.actor_agent_id}
        # Load the actors once and keep strong references (the identity map is weak), so
        # actors.out_legacy's db.get calls are identity-map hits, not one query per row.
        if user_ids:
            self._actors += db.scalars(select(User).where(User.id.in_(list(user_ids)))).all()
        if agent_ids:
            self._actors += db.scalars(select(AgentProfile).where(AgentProfile.id.in_(list(agent_ids)))).all()

    def submission_for(self, r: WorkflowResource, nt: CollaborationNodeTask | None) -> TaskSubmission | None:
        if r.submission_id and r.submission_id in self.subs:
            return self.subs[r.submission_id]
        if nt is None:
            return None
        candidates = [s for s in self.by_task.get(nt.task_id, []) if r.asset_version_id in self.sub_versions.get(s.id, set())]
        if not candidates:
            return None
        return max(candidates, key=lambda s: (s.seq, s.submitted_at or s.seq))


def _empty_acceptance(status: str) -> dict[str, Any]:
    return {"status": status, "current": False, "submission_id": None, "request_id": None, "decision_id": None, "decision": None,
            "actor_id": None, "actor_kind": None, "actor_user_id": None, "actor_agent_id": None, "actor": None, "decided_at": None}


def _acceptance(db: Session, r: WorkflowResource, nt: CollaborationNodeTask | None, ctx: _AcceptanceContext, superseded: bool) -> dict[str, Any]:
    if nt is None:
        return _empty_acceptance("not_applicable")
    invalidated = r.kind == "superseded" or (r.kind == "formal" and superseded)
    sub = ctx.submission_for(r, nt)
    if sub is None:
        return _empty_acceptance("invalidated" if invalidated else "unsubmitted")
    status = SUBMISSION_STATUS_MAP.get(sub.status, "unsubmitted")
    req = ctx.request.get(sub.id)
    dec = ctx.decision.get(req.id) if req is not None else None
    out = _empty_acceptance(status)
    out["submission_id"] = str(sub.id)
    out["request_id"] = _s(req.id) if req else None
    if dec is not None:
        actor = actors.out_legacy(db, dec, "actor_id")
        out.update({
            "decision_id": str(dec.id),
            "decision": dec.decision,
            "actor_id": actor["id"] if actor else None,
            "actor_kind": dec.actor_kind,
            "actor_user_id": _s(dec.actor_id),
            "actor_agent_id": _s(dec.actor_agent_id),
            "actor": actor,
            "decided_at": _iso(dec.created_at),
        })
    if r.kind == "superseded" or (r.kind == "formal" and superseded):
        out["status"] = "invalidated"
        out["current"] = False
        return out
    if status == "approved" and r.kind != "formal":
        # Candidate / evidence / input rows only describe the submission they belong to: approved while it is
        # the node's accepted submission (a reopen or a newer acceptance clears that, and adds superseded rows
        # for the formal outputs only); afterwards neutral, never "invalidated" (that is for formal outputs).
        if nt.accepted_submission_id == sub.id:
            out["current"] = True
        else:
            out["status"] = "not_applicable"
        return out
    if status == "approved" and r.kind == "formal" and nt.accepted_submission_id == sub.id and r.slot_key:
        slot_ok = r.slot_key in {s.get("slot_key") for s in (nt.output_slots or []) if isinstance(s, dict)}
        formal = next((f for f in (nt.formal_outputs or []) if isinstance(f, dict) and f.get("slot_key") == r.slot_key), None)
        if formal is not None:
            match = str(formal.get("asset_version_id")) == str(r.asset_version_id) and formal.get("content_hash") == r.content_hash
        else:
            match = ctx.sub_hashes.get(sub.id, {}).get(r.asset_version_id) == r.content_hash
        out["current"] = bool(slot_ok and match)
    return out


# --- items ---------------------------------------------------------------------------------


def items_from_rows(db: Session, rows: list[Any]) -> list[dict[str, Any]]:
    """C10 item dicts for joined rows from `rows_for` / `page` (batched: labels + acceptance context)."""
    if not rows:
        return []
    labels = version_labels(db, [(v, asset.type) for (_, v, asset, *_rest) in rows])
    ctx = _AcceptanceContext(db, {nt.task_id for (_, _, _, _, _, nt, *_r) in rows if nt is not None})
    items = []
    for r, v, asset, run, crun, nt, task, node_status, producer_status, superseded in rows:
        items.append({
            "id": str(r.id),
            "run_id": str(r.run_id),
            "project_id": str(r.project_id),
            "node_task_id": _s(r.node_task_id),
            "node_run_id": _s(r.node_run_id),
            "agent_execution_id": _s(r.agent_execution_id),
            "submission_id": _s(r.submission_id),
            "attempt": r.attempt,
            "asset_version_id": str(r.asset_version_id),
            "content_hash": r.content_hash,
            "slot_key": r.slot_key,
            "kind": r.kind,
            "metadata": r.meta or {},
            "created_at": _iso(r.created_at),
            "asset": version_brief(v, asset, label=labels[v.id], labels=_labels_of(v, node_status, producer_status)),
            "run_title": run.title,
            "run_status": run.status,
            "node_id": nt.node_id if nt else None,
            "phase": nt.phase if nt else None,
            "task_id": _s(task.id) if task else None,
            "task_number": task.number if task else None,
            "task_key": f"T-{task.number}" if task else None,
            "task_title": task.title if task else None,
            "task_status": task.status if task else None,
            "execution_mode": crun.execution_mode if crun else None,
            "task_acceptance": _acceptance(db, r, nt, ctx, bool(superseded)),
        })
    return items


def items_for_resources(db: Session, resource_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    """C10-shaped items keyed by resource id (knowledge item views reuse the same projection)."""
    if not resource_ids:
        return {}
    rows = db.execute(_order(_joins(_rows_select()).where(WorkflowResource.id.in_(resource_ids)))).all()
    return {uuid.UUID(it["id"]): it for it in items_from_rows(db, rows)}


# --- public API used by the routers and other backends ---------------------------------------


def task_acceptance(db: Session, *, node_task: CollaborationNodeTask, version_id: uuid.UUID) -> dict[str, Any]:
    """§4 C10 projection for one version in one node (the formal row when there is one)."""
    rows = rows_for(db, {"node_task_id": node_task.id, "asset_version_id": version_id})
    if not rows:
        ctx = _AcceptanceContext(db, {node_task.task_id})
        probe = WorkflowResource(run_id=node_task.run_id, node_task_id=node_task.id, asset_version_id=version_id, kind="candidate",
                                 content_hash="", submission_id=None)
        return _acceptance(db, probe, node_task, ctx, False)
    rows.sort(key=lambda row: 0 if row[0].kind == "formal" else 1)
    return items_from_rows(db, rows[:1])[0]["task_acceptance"]


def resources_of_run(db: Session, run_id: uuid.UUID, **filters: Any) -> list[dict[str, Any]]:
    """C08 items of one run (unpaged unless limit/offset are given)."""
    limit = filters.pop("limit", None)
    offset = filters.pop("offset", 0) or 0
    return items_from_rows(db, rows_for(db, {"run_id": run_id, **filters}, limit=limit, offset=offset))


def run_resources(db: Session, run: WorkflowRun, *, node_id: str | None = None, kind: str | None = None, asset_type: str | None = None,
                  limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """C08 response for one run."""
    return page(db, {"run_id": run.id, "node_id": node_id, "kind": kind, "asset_type": asset_type}, limit=limit, offset=offset)


def resource_summary(db: Session, run_id: uuid.UUID) -> dict[str, Any]:
    """{by_kind: {six kinds}, total} for the C03 projection."""
    by_kind = facets(db, {"run_id": run_id})
    return {"by_kind": by_kind, "total": sum(by_kind.values())}


def project_resources(db: Session, user: User, project_id: uuid.UUID, **query: Any) -> dict[str, Any]:
    """C10: project-wide resource query. `query` keys: q, run_id, task_id, node_id, asset_version_id, kind,
    asset_type, availability, publish_state, task_status, limit, offset. Read-only."""
    project_access(db, user, project_id)
    limit = int(query.pop("limit", 50) or 50)
    offset = int(query.pop("offset", 0) or 0)
    if not 1 <= limit <= MAX_LIMIT or offset < 0:
        raise ValueError("limit must be 1-100 and offset >= 0")
    run_id = query.get("run_id")
    if run_id is not None:
        run = db.get(WorkflowRun, run_id)
        if run is None or run.project_id != project_id:
            raise not_found("run")
    task_id = query.get("task_id")
    if task_id is not None:
        task = db.get(Task, task_id)
        if task is None or task.project_id != project_id:
            raise not_found("task")
    q = (query.get("q") or "").strip()
    f = {**query, "project_id": project_id, "q": q[:200]}
    return page(db, f, limit=limit, offset=offset)
