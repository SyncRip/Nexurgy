"""Authorized, read-only inbox projection over the existing decision owners.

No source is capped before counting. Private operations only influence the owner's
location; another member sees the ordinary run without private identifiers.
"""
from __future__ import annotations

import base64
import binascii
import json
import uuid
from datetime import UTC, datetime
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..errors import AppError
from ..models import (AiConversation, AiOperation, ApprovalRequest, Assembly, AssemblyRevision, Asset,
                      AssetDependency, AssetVersion, CollaborationRun, Drawing, EngineeringSpec,
                      EngineeringSpecVersion, ModelRevision, NodeRun, Part, Project, ProjectMember,
                      RunInputBinding, User, Workflow, WorkflowRun)
from ..security import ProjectAccess, project_access
from . import approval_requests, assets, engineering_spec, feature_flags, verification

SOURCES = ("engineering_approval", "agent_confirmation", "question", "spec_question", "turbo_assumption")
LIVE_RUNS = ("queued", "running", "waiting")
OWNER_MODELS = {"model_revision": ModelRevision, "drawing": Drawing,
                "assembly_revision": AssemblyRevision, "engineering_spec_version": EngineeringSpecVersion}


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat()


def _cursor(raw: str | None, scope: dict, now: datetime) -> tuple[datetime, tuple[str, str] | None]:
    if raw is None:
        return now, None
    try:
        if len(raw) > 4096:
            raise ValueError
        data = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True))
        if not isinstance(data, dict) or data.get("v") != 1 or data.get("scope") != scope:
            raise ValueError
        watermark = datetime.fromisoformat(data["watermark"])
        after = data["after"]
        if watermark.tzinfo is None or not isinstance(after, list) or len(after) != 2:
            raise ValueError
        created = datetime.fromisoformat(after[0])
        if created.tzinfo is None or created > watermark or not isinstance(after[1], str) or not after[1]:
            raise ValueError
        return watermark, (_iso(created), after[1])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeError):
        raise AppError(422, "invalid_cursor", "reload the first page of this inbox") from None


def _item(source, source_id, acc, obj, version, title, reason, at, location, *, updated=None, target_hash=None, count=1):
    return {"key": f"{source}:{source_id}", "source": source, "source_id": source_id,
            "project": {"id": str(acc.project.id), "code": acc.project.code, "name": acc.project.name},
            "object": obj, "version": version, "title": str(title),
            "reason": {"code": reason, "summary": str(title), "count": count},
            "created_at": _iso(at), "updated_at": _iso(updated or at), "target_hash": target_hash,
            "location": location, "allowed_actions": ["open"]}


def _object(kind, row, label):
    return {"type": kind, "id": str(row.id), "label": label}


def _version(kind, row, label=None):
    return {"type": kind, "id": str(row.id), "label": label}


def _retained(db, run, collabs) -> bool:
    if run.task_id is not None or run.id in collabs or run.status not in LIVE_RUNS or run.cancel_requested:
        return False
    if any(n.get("type") == "business_task" for n in (run.graph_snapshot or {}).get("nodes") or []):
        return False
    if run.workflow_id:
        wf = db.get(Workflow, run.workflow_id)
        if wf and (wf.kind != "tool" or wf.archived_at is not None):
            return False
    if run.part_id:
        part = db.get(Part, run.part_id)
        if part is None or part.archived_at is not None:
            return False
    return True


def _owner(db, kind, ident, project_id):
    """A true decision owner which is still available in this project."""
    model = OWNER_MODELS.get(kind)
    ident = _uuid(ident)
    row = db.get(model, ident) if model and ident else None
    if row is None:
        return None
    if kind == "engineering_spec_version":
        parent = db.get(EngineeringSpec, row.spec_id)
        usable = engineering_spec.effective_status(row)[0] in ("ready", "needs_answers")
    elif kind == "assembly_revision":
        parent = db.get(Assembly, row.assembly_id)
        usable = row.status == "ready"
    else:
        parent = db.get(Part, row.part_id)
        usable = row.status == "ready" if kind == "model_revision" else row.kind == "generated"
    if not usable or parent is None or parent.project_id != project_id or parent.archived_at is not None:
        return None
    return row, parent


def _approval_items(db, user, accesses, runs, roots):
    items = []
    pending = db.scalars(select(ApprovalRequest).where(ApprovalRequest.project_id.in_(accesses),
                        ApprovalRequest.kind == "engineering", ApprovalRequest.status == "pending",
                        ApprovalRequest.task_id.is_(None))).all()
    versions = []
    for req in pending:
        acc = accesses[req.project_id]
        if req.run_id and req.run_id not in runs:
            continue
        v = db.get(AssetVersion, req.asset_version_id) if req.asset_version_id else None
        identity = approval_requests.subject_identity(v) if v else None
        asset = db.get(Asset, v.asset_id) if v else None
        if identity is None or asset is None or asset.archived_at is not None or asset.project_id != req.project_id:
            continue
        kind, ident = identity
        row = db.get(assets.KIND_MODEL[approval_requests.SUBJECT_ASSET[kind]], ident)
        part = db.get(Part, row.part_id) if row else None
        if part is None or part.archived_at is not None or part.project_id != req.project_id:
            continue
        # Inputs stay relevant even if this particular member is not the reviewer.
        versions.append((v.id, req.project_id))
        if not approval_requests.can_decide(db, user, req, acc.role, acc.project):
            continue
        items.append(_item("engineering_approval", str(req.id), acc, _object("part", part, part.name),
                           _version(kind, row, f"v{v.version_no}"), req.title or "Engineering review", "engineering_review",
                           req.created_at, {"href": f"/approval-requests/{req.id}", "request_id": str(req.id)},
                           target_hash=req.target_hash))
    _asset_roots(db, versions, roots)
    return items


def _operations(db, user, accesses):
    return db.execute(select(AiOperation, AiConversation).join(AiConversation, AiConversation.id == AiOperation.conversation_id)
                      .where(AiOperation.project_id.in_(accesses), AiOperation.user_id == user.id,
                             AiConversation.user_id == user.id, AiConversation.archived_at.is_(None),
                             AiConversation.task_id.is_(None), AiOperation.task_id.is_(None),
                             AiOperation.status == "waiting_input").order_by(AiOperation.created_at.desc(), AiOperation.id)).all()


def _confirmations(db, accesses, operations):
    from .orchestrator_tools import TOOLS, TOOL_FEATURES

    items = []
    for op, conv in operations:
        q = op.question or {}
        if op.project_id != conv.project_id or q.get("kind") != "confirm" or op.run_id is not None:
            continue
        index = q.get("step")
        if type(index) is not int:
            continue
        step = next((s for s in (op.result or {}).get("steps") or [] if isinstance(s, dict) and s.get("index") == index), None)
        if not step or step.get("status") != "waiting_confirm" or step.get("tool") != q.get("tool"):
            continue
        name = step.get("tool")
        tool = TOOLS.get(name)
        acc = accesses[op.project_id]
        if tool is None or not acc.can(tool.action) or (name in TOOL_FEATURES and not feature_flags.enabled(TOOL_FEATURES[name])):
            continue
        items.append(_item("agent_confirmation", f"operation:{op.id}:step:{index}", acc,
                           _object("conversation", conv, conv.title), _version("ai_operation", op),
                           q.get("title") or "Confirm Agent action", "tool_confirmation", op.created_at,
                           {"href": f"/assistant/{conv.id}?operation={op.id}&step={index}",
                            "conversation_id": str(conv.id), "operation_id": str(op.id), "step": index}, updated=op.updated_at))
    return items


def _questions(db, accesses, runs, operations):
    own = {}
    for op, conv in operations:
        q = op.question or {}
        if op.project_id == conv.project_id and op.run_id in runs and q.get("kind") == "clarification" and q.get("node_id"):
            own.setdefault((op.run_id, q["node_id"]), (op, conv))
    items = []
    for node in db.scalars(select(NodeRun).where(NodeRun.run_id.in_(runs), NodeRun.status == "waiting_human")):
        run = runs[node.run_id]
        acc = accesses[run.project_id]
        if not acc.can("design") or run.paused:
            continue
        out = node.outputs or {}
        questions = (out.get("clarification") or {}).get("questions") or []
        if node.node_type in ("model_3d", "drawing_reconstruct") and questions:
            reason, title, count = "clarification", str(questions[0]), len(questions)
        elif node.node_type == "drawing_intake" and out.get("manual_entry"):
            reason, title, count = "manual_entry", "Complete drawing intake", 1
        else:
            continue
        loc = {"href": f"/runs/{run.id}?node={quote(node.node_id, safe='')}", "run_id": str(run.id),
               "node_run_id": str(node.id), "node_id": node.node_id}
        if reason == "clarification" and (run.id, node.node_id) in own:
            op, conv = own[(run.id, node.node_id)]
            loc.update(href=f"/assistant/{conv.id}?operation={op.id}", conversation_id=str(conv.id), operation_id=str(op.id))
        items.append(_item("question", f"node:{node.id}", acc, _object("run", run, run.title),
                           _version("workflow_run", run), title, reason, run.created_at, loc,
                           updated=node.updated_at, count=count))
    # No-run clarification JSON alone is not actionable: assistant.answer requires
    # a run. Legacy choice cards have no operation and are handled in the conversation.
    return items


def _spec_items(db, accesses, roots):
    rows = db.scalars(select(EngineeringSpecVersion).join(EngineeringSpec, EngineeringSpec.id == EngineeringSpecVersion.spec_id)
                      .where(EngineeringSpec.project_id.in_(accesses), EngineeringSpec.archived_at.is_(None))
                      .distinct(EngineeringSpecVersion.spec_id)
                      .order_by(EngineeringSpecVersion.spec_id, EngineeringSpecVersion.version_no.desc())).all()
    items = []
    for v in rows:
        if engineering_spec.effective_status(v)[0] not in ("ready", "needs_answers"):
            continue
        roots[("engineering_spec_version", v.id)] = v.project_id
        acc = accesses[v.project_id]
        if not acc.can("workflow"):
            continue
        questions = [q for q in (v.content or {}).get("questions") or [] if isinstance(q, dict) and q.get("id") and not q.get("answer")]
        if not questions:
            continue
        s = db.get(EngineeringSpec, v.spec_id)
        items.append(_item("spec_question", str(v.id), acc, _object("engineering_spec", s, s.title),
                           _version("engineering_spec_version", v, f"v{v.version_no}"), s.title, "spec_questions", v.created_at,
                           {"href": f"/specs/{s.id}?version={v.id}", "spec_version_id": str(v.id),
                            "question_ids": [str(q["id"]) for q in questions]}, updated=v.updated_at,
                           target_hash=verification.spec_sha(v), count=len(questions)))
    return items


def _asset_roots(db, versions, roots):
    seen = set()
    stack = list(versions)
    while stack:
        vid, pid = stack.pop()
        if (vid, pid) in seen:
            continue
        seen.add((vid, pid))
        v = db.get(AssetVersion, vid)
        asset = db.get(Asset, v.asset_id) if v else None
        if asset is None or asset.project_id != pid or asset.archived_at is not None:
            continue
        for kind, field in (("model_revision", "model_revision_id"), ("drawing", "drawing_id"), ("assembly_revision", "assembly_revision_id")):
            ident = getattr(v, field)
            if ident:
                roots[(kind, ident)] = pid
        stack.extend((ident, pid) for ident in db.scalars(select(AssetDependency.to_version_id)
                                                       .where(AssetDependency.from_version_id == v.id)))


def _reference_roots(value, pid, roots):
    """Only known exact typed inputs; strings in request text are never interpreted."""
    fields = {"revision_id": "model_revision", "model_revision_id": "model_revision", "drawing_id": "drawing",
              "spec_version_id": "engineering_spec_version", "assembly_revision_id": "assembly_revision"}
    if isinstance(value, dict):
        for key, item in value.items():
            kind = fields.get(key)
            ident = _uuid(item) if kind else None
            if ident:
                roots[(kind, ident)] = pid
            if isinstance(item, (dict, list)):
                _reference_roots(item, pid, roots)
    elif isinstance(value, list):
        for item in value:
            _reference_roots(item, pid, roots)


def _current_roots(db, accesses, runs, roots):
    for row, pid in db.execute(select(ModelRevision, Part.project_id).join(Part, Part.id == ModelRevision.part_id)
                               .where(Part.project_id.in_(accesses), Part.archived_at.is_(None), ModelRevision.status == "ready")
                               .distinct(ModelRevision.part_id).order_by(ModelRevision.part_id, ModelRevision.rev_no.desc())):
        roots[("model_revision", row.id)] = pid
    for row, pid in db.execute(select(AssemblyRevision, Assembly.project_id).join(Assembly, Assembly.id == AssemblyRevision.assembly_id)
                               .where(Assembly.project_id.in_(accesses), Assembly.archived_at.is_(None), AssemblyRevision.status == "ready")
                               .distinct(AssemblyRevision.assembly_id).order_by(AssemblyRevision.assembly_id, AssemblyRevision.rev_no.desc())):
        roots[("assembly_revision", row.id)] = pid
    heads = select(AssetVersion.asset_id, func.max(AssetVersion.version_no).label("n")).group_by(AssetVersion.asset_id).subquery()
    for v, pid in db.execute(select(AssetVersion, Asset.project_id).join(Asset, Asset.id == AssetVersion.asset_id)
                             .join(heads, (heads.c.asset_id == Asset.id) & (heads.c.n == AssetVersion.version_no))
                             .where(Asset.project_id.in_(accesses), Asset.type == "drawing", Asset.archived_at.is_(None),
                                    AssetVersion.availability == "ready")):
        if v.drawing_id:
            roots[("drawing", v.drawing_id)] = pid
    inputs = [(b.asset_version_id, runs[b.run_id].project_id) for b in db.scalars(select(RunInputBinding).where(RunInputBinding.run_id.in_(runs)))]
    _asset_roots(db, inputs, roots)
    for run in runs.values():
        for node in (run.graph_snapshot or {}).get("nodes") or []:
            _reference_roots(node.get("params") or {}, run.project_id, roots)
    for node in db.scalars(select(NodeRun).where(NodeRun.run_id.in_(runs))):
        _reference_roots(node.inputs, runs[node.run_id].project_id, roots)


def _assumptions(db, accesses, roots):
    items = {}
    for (kind, ident), pid in roots.items():
        acc = accesses[pid]
        if not acc.can("design") or _owner(db, kind, ident, pid) is None:
            continue
        if kind == "drawing":
            row = db.get(Drawing, ident)
            pending = verification.drawing_verification(row, db)["unconfirmed_assumptions"]
        else:
            pending = [a for a in verification.upstream_assumptions(db, project_id=pid, source_type=kind, source_id=ident)["items"]
                       if a["state"] == "unconfirmed"]
        for assumption in pending:
            owner = assumption.get("owner") or {}
            owner_kind, oid, aid = owner.get("type"), owner.get("id"), assumption.get("id")
            actual = _owner(db, owner_kind, oid, pid)
            if actual is None or not aid:
                continue
            row, parent = actual
            if owner_kind == "engineering_spec_version":
                obj = _object("engineering_spec", parent, parent.title)
                path, target_hash, label = f"/specs/{parent.id}?version={row.id}", verification.spec_sha(row), f"v{row.version_no}"
            elif owner_kind == "assembly_revision":
                obj = _object("assembly", parent, parent.name)
                path, target_hash, label = f"/assemblies/{parent.id}/structure?rev={row.id}", row.content_sha256, f"A{row.rev_no}"
            elif owner_kind == "drawing":
                obj = _object("part", parent, parent.name)
                path, target_hash, label = f"/parts/{parent.id}/drawings?drawing={row.id}", row.manifest_sha256, None
            else:
                obj = _object("part", parent, parent.name)
                path, target_hash, label = f"/parts/{parent.id}/model?rev={row.id}", row.step_sha, f"R{row.rev_no}"
            sid = f"{owner_kind}:{row.id}:{aid}"
            items[sid] = _item("turbo_assumption", sid, acc, obj, _version(owner_kind, row, label),
                               assumption.get("subject") or assumption.get("field") or "Confirm assumption", "assumption_unconfirmed",
                               row.created_at, {"href": f"{path}&assumption={quote(str(aid), safe='')}", "assumption_id": str(aid)},
                               updated=getattr(row, "updated_at", None), target_hash=target_hash)
    return list(items.values())


def listing(db: Session, user: User, *, project_id: uuid.UUID | None = None, limit=50, cursor=None):
    now = datetime.now(UTC)
    if project_id is not None:
        project_access(db, user, project_id)
    scope = {"user_id": str(user.id), "project_id": str(project_id) if project_id else None}
    watermark, after = _cursor(cursor, scope, now)
    stmt = select(Project, ProjectMember).join(ProjectMember, ProjectMember.project_id == Project.id).where(
        ProjectMember.user_id == user.id, Project.status == "active", Project.archived_at.is_(None),
        Project.lifecycle.in_(("active", "on_hold")))
    if project_id is not None:
        stmt = stmt.where(Project.id == project_id)
    accesses = {p.id: ProjectAccess(p, user, m.role, tuple(m.capabilities or ())) for p, m in db.execute(stmt)}
    collabs = set(db.scalars(select(CollaborationRun.run_id).join(WorkflowRun, WorkflowRun.id == CollaborationRun.run_id)
                             .where(WorkflowRun.project_id.in_(accesses))))
    runs = {r.id: r for r in db.scalars(select(WorkflowRun).where(WorkflowRun.project_id.in_(accesses), WorkflowRun.status.in_(LIVE_RUNS)))
            if _retained(db, r, collabs)}
    roots = {}
    operations = _operations(db, user, accesses)
    items = _approval_items(db, user, accesses, runs, roots)
    items += _confirmations(db, accesses, operations)
    items += _questions(db, accesses, runs, operations)
    items += _spec_items(db, accesses, roots)
    _current_roots(db, accesses, runs, roots)
    items += _assumptions(db, accesses, roots)
    items = list({i["key"]: i for i in items}.values())
    counts = {source: sum(i["source"] == source for i in items) for source in SOURCES}
    eligible = [i for i in items if i["created_at"] <= _iso(watermark) and
                (after is None or i["created_at"] < after[0] or (i["created_at"] == after[0] and i["key"] > after[1]))]
    eligible.sort(key=lambda i: i["key"])
    eligible.sort(key=lambda i: i["created_at"], reverse=True)
    page = eligible[:limit]
    next_cursor = None
    if len(eligible) > limit:
        payload = {"v": 1, "scope": scope, "watermark": _iso(watermark), "after": [page[-1]["created_at"], page[-1]["key"]]}
        next_cursor = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    return {"items": page, "total": len(items), "counts": counts, "next_cursor": next_cursor, "as_of": _iso(now)}
