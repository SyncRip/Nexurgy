"""Deliverable submissions and their checks (design §6.3–6.4; contract §3).

A submission freezes the criteria, the chosen asset versions, their content
hashes and source chain, the engineering approvals and the check results into
`snapshot`; `snapshot_hash` is the sha256 of its canonical JSON and is the
`target_hash` of the task_acceptance request. Checks run at submission and
again at approval (with the frozen criteria and versions), so a revoked
engineering approval or a changed version blocks completion.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..errors import AppError, conflict, forbidden, not_found
from ..models import (
    AcceptanceCriterion,
    ApprovalRequest,
    Asset,
    AssetVersion,
    AuditLog,
    Drawing,
    ProcessPlan,
    Quote,
    SubmissionItem,
    Task,
    TaskBaselineSnapshot,
    TaskDeliverableDraft,
    TaskSubmission,
    User,
)
from ..security import ProjectAccess, project_access
from . import feature_flags as features
from . import approvals
from . import outbox
from .actors import assign_legacy
from .audit import audit
from .lifecycle import require_active
from .tasks import assets, baseline_items, bump, can_edit, canon_hash, check_version, criterion_out, lock_task, require_participant

POLICY_VERSION = "policy-2026.10-1"
DESIGN_TYPES = frozenset({"part_model", "intake"})
DERIVED_TYPES = frozenset({"drawing", "process_plan", "quote"})
# asset type -> (approval subject type, AssetVersion column)
SUBJECT_OF_TYPE = {
    "part_model": ("model_revision", "model_revision_id"),
    "drawing": ("drawing", "drawing_id"),
    "process_plan": ("process_plan", "process_plan_id"),
    "quote": ("quote", "quote_id"),
    "intake": ("intake", "intake_id"),
}
ENGINEERING_STAGES = {"part_model": "design", "drawing": "design", "intake": "design", "process_plan": "process", "quote": "quote"}


def allow_self_review() -> bool:
    return bool(getattr(get_settings(), "allow_self_review", False))


# --- asset helpers ----------------------------------------------------------------


class _Versions:
    """Small per-check cache of versions, their asset types and labels."""

    def __init__(self, db: Session):
        self.db = db
        self._v: dict[uuid.UUID, AssetVersion | None] = {}
        self._type: dict[uuid.UUID, str | None] = {}
        self._out: dict[uuid.UUID, dict] = {}
        self._labels: dict[uuid.UUID, list[str]] = {}

    def labels(self, vid: uuid.UUID) -> list[str]:
        """Stored labels plus read-time ones (from_failed_run) — never version.labels alone."""
        if vid not in self._labels:
            v = self.get(vid)
            self._labels[vid] = list(assets().labels_of(self.db, v)) if v is not None else []
        return self._labels[vid]

    def get(self, vid: uuid.UUID) -> AssetVersion | None:
        if vid not in self._v:
            self._v[vid] = self.db.get(AssetVersion, vid)
        return self._v[vid]

    def type(self, vid: uuid.UUID) -> str | None:
        if vid not in self._type:
            v = self.get(vid)
            a = self.db.get(Asset, v.asset_id) if v else None
            self._type[vid] = a.type if a else None
        return self._type[vid]

    def label(self, vid: uuid.UUID) -> str:
        out = self.out(vid)
        return str(out.get("label") or out.get("version_no") or vid)

    def out(self, vid: uuid.UUID) -> dict:
        if vid not in self._out:
            v = self.get(vid)
            if v is None:
                self._out[vid] = {"id": str(vid), "missing": True}
            else:
                try:
                    self._out[vid] = assets().version_out(self.db, v)
                except Exception:  # noqa: BLE001 - catalogue view is informative only
                    self._out[vid] = {"id": str(v.id), "version_no": v.version_no}
        return self._out[vid]


def _direct_sources(db: Session, cache: _Versions, vid: uuid.UUID) -> list[uuid.UUID]:
    """Versions this one was derived from: catalogue edges first, else the
    specialised row's own references (drawing→revision, plan→revision/intake,
    quote→plan)."""
    mod = assets()
    edges = mod.dependencies(db, vid)
    if edges:
        return [e.to_version_id for e in edges]
    v = cache.get(vid)
    if v is None:
        return []
    refs: list[tuple[str, uuid.UUID | None]] = []
    if v.drawing_id:
        d = db.get(Drawing, v.drawing_id)
        refs.append(("part_model", d.model_revision_id if d else None))
    elif v.process_plan_id:
        p = db.get(ProcessPlan, v.process_plan_id)
        if p:
            refs += [("part_model", p.model_revision_id), ("intake", p.intake_id)]
    elif v.quote_id:
        q = db.get(Quote, v.quote_id)
        refs.append(("process_plan", q.process_plan_id if q else None))
    out = []
    for kind, row_id in refs:
        if row_id:
            src = mod.version_for(db, kind, row_id)
            if src is not None:
                out.append(src.id)
    return out


def design_sources(db: Session, vid: uuid.UUID, cache: _Versions | None = None) -> tuple[set[uuid.UUID], list[uuid.UUID]]:
    """Trace a drawing / plan / quote back to the design version(s) it rests on
    (model revision or intake). Returns (design roots, full chain)."""
    cache = cache or _Versions(db)
    roots: set[uuid.UUID] = set()
    chain: list[uuid.UUID] = []
    seen = {vid}
    frontier = [vid]
    while frontier:
        cur = frontier.pop()
        for src in _direct_sources(db, cache, cur):
            if src in seen:
                continue
            seen.add(src)
            chain.append(src)
            if cache.type(src) in DESIGN_TYPES:
                roots.add(src)  # stop: a revision's own parents are history, not sources
            else:
                frontier.append(src)
    return roots, chain


def consistency_issues(
    db: Session,
    version_ids: list[uuid.UUID],
    baseline: dict[str, uuid.UUID],
    slot_for: dict[uuid.UUID, str] | None = None,
    cache: _Versions | None = None,
) -> list[dict]:
    """Drawings, plans and quotes must rest on the baseline's slot version
    (target_model by default) or on a design version delivered with them.
    Without either reference they must at least agree with each other."""
    cache = cache or _Versions(db)
    slot_for = slot_for or {}
    delivered_designs = {v for v in version_ids if cache.type(v) in DESIGN_TYPES}
    derived = [v for v in version_ids if cache.type(v) in DERIVED_TYPES]
    traced = {v: design_sources(db, v, cache)[0] for v in derived}
    issues: list[dict] = []
    all_roots = set().union(*traced.values()) if traced else set()
    for v in derived:
        roots = traced[v]
        slot = slot_for.get(v, "target_model")
        reference = ({baseline[slot]} if slot in baseline else set()) | delivered_designs
        base = {"asset_version_id": str(v), "label": cache.label(v), "asset_type": cache.type(v), "slot": slot,
                "sources": [str(r) for r in sorted(roots, key=str)], "source_labels": [cache.label(r) for r in sorted(roots, key=str)],
                "expected": [str(r) for r in sorted(reference, key=str)], "expected_labels": [cache.label(r) for r in sorted(reference, key=str)]}
        if not roots:
            issues.append({**base, "code": "source_unknown", "blocking": False,
                           "message": "the design version this item was made from is not recorded"})
        elif reference and not roots <= reference:
            issues.append({**base, "code": "inconsistent_source", "blocking": True,
                           "message": f"{cache.label(v)} was made from {', '.join(base['source_labels'])}, not from the baseline or delivered design ({', '.join(base['expected_labels'])})"})
        elif not reference and len(all_roots) > 1:
            issues.append({**base, "code": "mixed_sources", "blocking": True,
                           "message": "the delivered items were made from different design versions"})
    return issues


# --- checks -------------------------------------------------------------------------


def _criterion_types(c: dict) -> set[str]:
    return set(c.get("allowed_types") or []) | set((c.get("rule_spec") or {}).get("asset_types") or [])


def run_checks(
    db: Session,
    *,
    criteria: list[dict],
    items: list[dict],
    baseline: dict[str, uuid.UUID],
    frozen_hashes: dict[uuid.UUID, str] | None = None,
) -> dict:
    """Evaluate the closed rule set. `criteria` are criterion_out dicts (live or
    frozen); `items` are {asset_version_id, criterion_ids}. Blocking: any
    problem of a required criterion, or an item that is not ready, changed or
    inconsistent."""
    cache = _Versions(db)
    vids = [uuid.UUID(str(i["asset_version_id"])) for i in items]
    crit_by_id = {str(c["id"]): c for c in criteria}
    slot_for: dict[uuid.UUID, str] = {}
    for i in items:
        for cid in i.get("criterion_ids") or []:
            slot = ((crit_by_id.get(str(cid)) or {}).get("rule_spec") or {}).get("consistent_with_slot")
            if slot:
                slot_for.setdefault(uuid.UUID(str(i["asset_version_id"])), slot)

    item_reports: dict[uuid.UUID, dict] = {}
    for vid in vids:
        v = cache.get(vid)
        problems: list[dict] = []
        warnings: list[dict] = []
        if v is None:
            problems.append({"code": "missing_version", "message": "the asset version no longer exists"})
        else:
            if v.availability != "ready":
                problems.append({"code": "not_ready", "message": f"the version is {v.availability}"})
            if frozen_hashes and frozen_hashes.get(vid) and frozen_hashes[vid] != v.content_hash:
                problems.append({"code": "content_changed", "message": "the version content changed after submission"})
            if "unverified" in cache.labels(vid):  # always flagged; refused by a criterion unless it allows it
                warnings.append({"code": "unverified", "message": "not verified: Turbo output or AI values no person confirmed"})
        item_reports[vid] = {"asset_version_id": str(vid), "label": cache.label(vid), "asset_type": cache.type(vid),
                             "labels": cache.labels(vid), "problems": problems, "warnings": warnings}
    for issue in consistency_issues(db, vids, baseline, slot_for, cache):
        rep = item_reports[uuid.UUID(issue["asset_version_id"])]
        (rep["problems"] if issue["blocking"] else rep["warnings"]).append(issue)
    for rep in item_reports.values():
        rep["ok"] = not rep["problems"]

    crit_reports = []
    for c in sorted(criteria, key=lambda x: x.get("seq", 0)):
        spec = c.get("rule_spec") or {}
        types = _criterion_types(c)
        evidence = [uuid.UUID(str(i["asset_version_id"])) for i in items if str(c["id"]) in {str(x) for x in i.get("criterion_ids") or []}]
        problems: list[dict] = []
        typed = []
        for vid in evidence:
            t = cache.type(vid)
            if types and t not in types:
                problems.append({"code": "type_not_allowed", "asset_version_id": str(vid), "label": cache.label(vid),
                                 "message": f"{t} is not accepted here (allowed: {', '.join(sorted(types))})"})
                continue
            typed.append(vid)
            v = cache.get(vid)
            labels = set(cache.labels(vid))
            if v is not None and v.availability != "ready":
                problems.append({"code": "evidence_not_ready", "asset_version_id": str(vid), "label": cache.label(vid), "message": "the version is not ready"})
            if "estimated" in labels and not spec.get("allow_estimated"):
                problems.append({"code": "estimated_not_allowed", "asset_version_id": str(vid), "label": cache.label(vid),
                                 "message": "estimated data is not accepted by this criterion"})
            if "simulated" in labels and not spec.get("allow_simulated"):
                problems.append({"code": "simulated_not_allowed", "asset_version_id": str(vid), "label": cache.label(vid),
                                 "message": "simulated AI output is not accepted by this criterion"})
            if "unverified" in labels and not spec.get("allow_unverified"):
                problems.append({"code": "unverified_not_allowed", "asset_version_id": str(vid), "label": cache.label(vid),
                                 "message": "an unverified result (Turbo, or AI values no person confirmed) is not accepted by this "
                                            "criterion; re-run the checks and confirm the assumptions first"})
            stage = spec.get("require_approval_stage")
            if stage and v is not None:
                subject = SUBJECT_OF_TYPE.get(t or "")
                if subject is None or subject[0] not in approvals.STAGE_SUBJECTS.get(stage, set()):
                    problems.append({"code": "approval_not_applicable", "asset_version_id": str(vid), "label": cache.label(vid),
                                     "message": f"a {t} cannot carry a {stage} approval"})
                elif not approvals.is_approved(db, subject[0], getattr(v, subject[1]), stage):
                    problems.append({"code": "approval_missing", "asset_version_id": str(vid), "label": cache.label(vid), "stage": stage,
                                     "message": f"{cache.label(vid)} has no valid {stage} approval"})
            slot = spec.get("consistent_with_slot")
            if slot and cache.type(vid) in DESIGN_TYPES and slot in baseline and baseline[slot] != vid:
                problems.append({"code": "not_baseline", "asset_version_id": str(vid), "label": cache.label(vid), "slot": slot,
                                 "message": f"{cache.label(vid)} is not the baseline {slot} ({cache.label(baseline[slot])})"})
        need = int(spec.get("min_count", 1))
        if len(typed) < need:
            problems.append({"code": "missing_evidence", "needed": need, "found": len(typed), "types": sorted(types),
                             "message": f"needs {need} item(s) of type {', '.join(sorted(types)) or 'any'}, has {len(typed)}"})
        required = bool(c.get("required", True))
        crit_reports.append({
            "criterion_id": str(c["id"]),
            "seq": c.get("seq"),
            "description": c.get("description", ""),
            "required": required,
            "manual_required": bool(c.get("manual_required")),
            "evidence": [str(v) for v in evidence],
            "problems": problems,
            "ok": not problems,
            "blocking": required and bool(problems),
        })
    ok = not any(r["blocking"] for r in crit_reports) and all(r["ok"] for r in item_reports.values())
    return {"ok": ok, "criteria": crit_reports, "items": list(item_reports.values()), "policy_version": POLICY_VERSION}


def _engineering(db: Session, cache: _Versions, vid: uuid.UUID) -> dict | None:
    v = cache.get(vid)
    t = cache.type(vid)
    if v is None or t not in SUBJECT_OF_TYPE:
        return None
    st, col = SUBJECT_OF_TYPE[t]
    stage = ENGINEERING_STAGES[t]
    return {"stage": stage, "approved": approvals.is_approved(db, st, getattr(v, col), stage)}


# --- output -------------------------------------------------------------------------


def submission_out(db: Session, s: TaskSubmission, *, with_snapshot: bool = True) -> dict:
    req = db.scalar(select(ApprovalRequest).where(ApprovalRequest.submission_id == s.id).order_by(ApprovalRequest.created_at.desc()).limit(1))
    data: dict[str, Any] = {
        "id": str(s.id),
        "task_id": str(s.task_id),
        "seq": s.seq,
        "baseline_snapshot_id": str(s.baseline_snapshot_id) if s.baseline_snapshot_id else None,
        "snapshot_hash": s.snapshot_hash,
        "note": s.note,
        "status": s.status,
        "submitted_by": str(s.submitted_by) if s.submitted_by else None,
        "submitted_by_kind": s.submitted_by_kind,
        "submitted_by_agent_id": str(s.submitted_by_agent_id) if s.submitted_by_agent_id else None,
        "submitted_at": s.submitted_at.isoformat() if s.submitted_at else None,
        "decided_at": s.decided_at.isoformat() if s.decided_at else None,
        "request_id": str(req.id) if req else None,
        "checks_ok": bool((s.snapshot or {}).get("checks", {}).get("ok")),
    }
    if with_snapshot:
        data["snapshot"] = s.snapshot
        data["items"] = [
            {"id": str(i.id), "asset_version_id": str(i.asset_version_id), "criterion_id": str(i.criterion_id) if i.criterion_id else None,
             "content_hash": i.content_hash, "check_result": i.check_result}
            for i in db.scalars(select(SubmissionItem).where(SubmissionItem.submission_id == s.id)).all()
        ]
    return data


# --- create / withdraw / decide -------------------------------------------------------


def _live_inputs(db: Session, t: Task) -> tuple[list[dict], list[TaskDeliverableDraft]]:
    crits = [criterion_out(c) for c in db.scalars(select(AcceptanceCriterion).where(AcceptanceCriterion.task_id == t.id).order_by(AcceptanceCriterion.seq)).all()]
    drafts = list(db.scalars(select(TaskDeliverableDraft).where(TaskDeliverableDraft.task_id == t.id).order_by(TaskDeliverableDraft.selected_at)).all())
    return crits, drafts


def preview_checks(db: Session, t: Task) -> dict:
    """Dry run of the submission checks on the current drafts."""
    crits, drafts = _live_inputs(db, t)
    items = [{"asset_version_id": d.asset_version_id, "criterion_ids": d.criterion_ids or []} for d in drafts]
    return run_checks(db, criteria=crits, items=items, baseline=baseline_items(db, t.current_baseline_id))


def create_submission(db: Session, acc: ProjectAccess, task_id: uuid.UUID, *, note: str, expected_version: int) -> TaskSubmission:
    """One transaction: lock task → freeze → checks → submission + items +
    task_acceptance request → task in_review, version+1. Caller commits."""
    user = acc.user
    require_active(db, acc.project.id, what="submitting deliverables")
    t = lock_task(db, task_id)
    from .agent_runtime import guard_generic_write

    guard_generic_write(db, t.id, "submit it")
    check_version(t, expected_version)
    require_participant(db, acc, t)
    features.require_write(db, "tasks", task_id=t.id)
    if t.status != "in_progress":
        raise conflict("task_not_in_progress", f"only an in-progress task can be submitted (it is {t.status})", status=t.status)
    if t.reviewer_id is None:
        raise AppError(422, "reviewer_required", "set the task reviewer before submitting")
    if t.reviewer_id == user.id and not allow_self_review():
        raise AppError(422, "self_review_forbidden", "you are the reviewer of this task; ask a project owner to assign another reviewer")
    return _submit(db, t, actor=None, user=user, note=note, created_by=user.id)


def create_submission_as_actor(db: Session, t: Task, *, actor: Any, note: str, defer_approvals: bool = True) -> TaskSubmission:
    """Submission by a non-HTTP actor (an executing agent of a collaboration node,
    design §3.8). The caller holds project(share) → task and validated the actor.
    The final reviewer is the node's reviewer (a person → assignee; an agent →
    no assignee, decided through approval_requests.decide_as_agent). Engineering
    approvals a criterion requires are re-checked at acceptance instead of
    blocking the submission (`defer_approvals`), so the independent reviewer
    can record them without a circular wait (requirements §3.2)."""
    from .agent_runtime import node_task_for_task
    from ..models import WorkflowRun

    features.require_write(db, "tasks", task_id=t.id)
    if t.status != "in_progress":
        raise conflict("task_not_in_progress", f"only an in-progress task can be submitted (it is {t.status})", status=t.status)
    nt = node_task_for_task(db, t.id)
    if nt is None:
        raise AppError(422, "not_collaboration", "only collaboration node tasks accept agent submissions")
    if nt.reviewer_kind == "human" and t.reviewer_id is None:
        raise AppError(422, "reviewer_required", "the node has no human reviewer")
    if actor.kind == "agent" and nt.reviewer_kind == "agent" and nt.reviewer_agent_id == actor.agent_profile_id:
        raise AppError(422, "self_review_forbidden", "the executing agent cannot be its own final reviewer")
    if actor.kind == "human" and t.reviewer_id == actor.user_id and not allow_self_review():
        raise AppError(422, "self_review_forbidden", "the submitter cannot accept their own submission")
    run = db.get(WorkflowRun, nt.run_id)
    assert run is not None
    return _submit(db, t, actor=actor, user=None, note=note, created_by=run.created_by, defer_approvals=defer_approvals)


def _defer_approval_problems(report: dict) -> dict:
    """Move `approval_missing` problems of criteria to `deferred` (re-checked at acceptance)."""
    for c in report["criteria"]:
        deferred = [p for p in c["problems"] if p.get("code") == "approval_missing"]
        if deferred:
            c["problems"] = [p for p in c["problems"] if p.get("code") != "approval_missing"]
            c["deferred"] = deferred
            c["ok"] = not c["problems"]
            c["blocking"] = bool(c.get("required", True)) and bool(c["problems"])
    report["ok"] = not any(r["blocking"] for r in report["criteria"]) and all(r["ok"] for r in report["items"])
    return report


def _submit(db: Session, t: Task, *, actor: Any, user: User | None, note: str, created_by: uuid.UUID, defer_approvals: bool = False) -> TaskSubmission:
    """Freeze, check and create the submission + items + request (task locked by the caller)."""
    features.require_write(db, "tasks", task_id=t.id)
    crits, drafts = _live_inputs(db, t)
    if not drafts:
        raise AppError(422, "no_deliverables", "select at least one deliverable version first")
    baseline = baseline_items(db, t.current_baseline_id)
    items = [{"asset_version_id": d.asset_version_id, "criterion_ids": d.criterion_ids or []} for d in drafts]
    report = run_checks(db, criteria=crits, items=items, baseline=baseline)
    if defer_approvals:
        report = _defer_approval_problems(report)
    if not report["ok"]:
        raise AppError(422, "submission_checks_failed", "some acceptance checks failed", {"checks": report})

    cache = _Versions(db)
    frozen_items = []
    for d in drafts:
        v = cache.get(d.asset_version_id)
        assert v is not None
        _, chain = design_sources(db, v.id, cache) if cache.type(v.id) in DERIVED_TYPES else (set(), [])
        frozen_items.append({
            "asset_version_id": str(v.id),
            "asset_id": str(v.asset_id),
            "asset_type": cache.type(v.id),
            "label": cache.label(v.id),
            "version_no": v.version_no,
            "content_hash": v.content_hash,
            "availability": v.availability,
            "labels": sorted(cache.labels(v.id)),
            "criterion_ids": [str(c) for c in d.criterion_ids or []],
            "note": d.note,
            "sources": [str(x) for x in chain],
            "engineering": _engineering(db, cache, v.id),
        })
    snap_row = db.get(TaskBaselineSnapshot, t.current_baseline_id) if t.current_baseline_id else None
    snapshot = {
        "schema": 1,
        "policy_version": POLICY_VERSION,
        "task": {"id": str(t.id), "number": t.number, "title": t.title, "objective": t.objective, "version": t.version},
        "baseline": {"id": str(snap_row.id), "seq": snap_row.seq, "hash": snap_row.hash,
                     "items": {k: str(v) for k, v in sorted(baseline.items())}} if snap_row else None,
        "criteria": crits,
        "items": frozen_items,
        "checks": report,
        "note": note,
        "submitted_by": str(user.id) if user is not None else None,
    }
    if actor is not None:
        snapshot["submitted_by_actor"] = actor.model_dump(mode="json")
    snapshot_hash = canon_hash(snapshot)
    seq = (db.scalar(select(func.max(TaskSubmission.seq)).where(TaskSubmission.task_id == t.id)) or 0) + 1
    sub = TaskSubmission(task_id=t.id, seq=seq, baseline_snapshot_id=t.current_baseline_id, snapshot=snapshot, snapshot_hash=snapshot_hash,
                         note=note, status="pending")
    if actor is not None:
        assign_legacy(sub, "submitted_by", actor)
    else:
        sub.submitted_by = user.id  # type: ignore[union-attr]
    db.add(sub)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        raise conflict("submission_pending", "the task already has a pending submission")
    crit_rep = {r["criterion_id"]: r for r in report["criteria"]}
    item_rep = {r["asset_version_id"]: r for r in report["items"]}
    for fi in frozen_items:
        cids = fi["criterion_ids"] or [None]
        for cid in cids:
            db.add(SubmissionItem(
                submission_id=sub.id,
                asset_version_id=uuid.UUID(fi["asset_version_id"]),
                criterion_id=uuid.UUID(cid) if cid else None,
                content_hash=fi["content_hash"],
                check_result={"item": item_rep.get(fi["asset_version_id"]), "criterion": crit_rep.get(cid) if cid else None},
            ))
    req = ApprovalRequest(project_id=t.project_id, kind="task_acceptance", submission_id=sub.id, target_hash=snapshot_hash,
                          policy_version=POLICY_VERSION, assignee_id=t.reviewer_id, status="pending", task_id=t.id,
                          title=f"T-{t.number} {t.title}"[:300], created_by=created_by)
    db.add(req)
    t.status = "in_review"
    bump(t)
    actor_user = user.id if user is not None else (actor.user_id if actor is not None else None)
    db.add(AuditLog(project_id=t.project_id, user_id=actor_user, agent_id=actor.agent_profile_id if actor is not None else None,
                    action="submission.create", subject_type="task", subject_id=str(t.id),
                    data={"submission_id": str(sub.id), "seq": seq, "snapshot_hash": snapshot_hash}))
    db.flush()
    outbox.request_opened(db, req, actor_user)
    outbox.task_status(db, t, actor_user, "in_progress")
    from .agent_runtime import on_submission_created

    on_submission_created(db, t, sub)
    return sub


def load_submission(db: Session, user: User, submission_id: uuid.UUID, action: str = "view") -> tuple[TaskSubmission, Task, ProjectAccess]:
    s = db.get(TaskSubmission, submission_id)
    if s is None:
        raise not_found("submission")
    t = db.get(Task, s.task_id)
    assert t is not None
    acc = project_access(db, user, t.project_id, action)
    return s, t, acc


def _lock_submission(db: Session, submission_id: uuid.UUID) -> TaskSubmission:
    s = db.scalar(select(TaskSubmission).where(TaskSubmission.id == submission_id).with_for_update().execution_options(populate_existing=True))
    assert s is not None
    return s


def withdraw(db: Session, user: User, submission_id: uuid.UUID) -> TaskSubmission:
    s, t, acc = load_submission(db, user, submission_id, "workflow")
    t = lock_task(db, t.id)
    from .agent_runtime import guard_generic_write

    guard_generic_write(db, t.id, "withdraw it")
    s = _lock_submission(db, s.id)
    if (s.submitted_by is None or s.submitted_by != user.id) and not can_edit(acc, t):
        raise forbidden("only the submitter, the task owner or a project owner can withdraw")
    features.require_write(db, "tasks", task_id=t.id)
    if s.status == "withdrawn":
        return s
    if s.status != "pending":
        raise conflict("submission_closed", f"the submission is already {s.status}", status=s.status)
    withdraw_locked(db, user, s, reason="withdrawn")
    return s


def withdraw_locked(db: Session, user: User, s: TaskSubmission, *, reason: str) -> None:
    """Withdraw a pending submission and its request; task back to in_progress."""
    t = lock_task(db, s.task_id)
    features.require_task_closure(db, t.id)
    now = datetime.now(UTC)
    s.status = "withdrawn"
    s.decided_at = now
    for req in db.scalars(select(ApprovalRequest).where(ApprovalRequest.submission_id == s.id, ApprovalRequest.status == "pending").with_for_update()).all():
        req.status = "withdrawn"
        req.closed_at = now
        req.version += 1
    if t.status == "in_review":
        t.status = "in_progress"
        bump(t)
        outbox.task_status(db, t, user.id, "in_review", reason)
    audit(db, action="submission.withdraw", subject_type="task", subject_id=t.id, user_id=user.id, project_id=t.project_id,
          data={"submission_id": str(s.id), "reason": reason})
    from .agent_runtime import on_submission_withdrawn

    on_submission_withdrawn(db, t, s)
    db.flush()


def recheck(db: Session, s: TaskSubmission) -> dict:
    """Checks again on the frozen criteria and versions (approval time)."""
    snap = s.snapshot or {}
    items = [{"asset_version_id": i["asset_version_id"], "criterion_ids": i.get("criterion_ids") or []} for i in snap.get("items", [])]
    frozen = {uuid.UUID(i["asset_version_id"]): i["content_hash"] for i in snap.get("items", [])}
    baseline = {k: uuid.UUID(v) for k, v in ((snap.get("baseline") or {}).get("items") or {}).items()}
    report = run_checks(db, criteria=snap.get("criteria", []), items=items, baseline=baseline, frozen_hashes=frozen)
    if canon_hash(snap) != s.snapshot_hash:
        report["ok"] = False
        report["snapshot_tampered"] = True
    return report


def accept(db: Session, s: TaskSubmission, t: Task) -> dict:
    """Approve: checks must still pass (409 submission_no_longer_valid otherwise)."""
    features.require_write(db, "tasks", task_id=t.id)
    report = recheck(db, s)
    if not report["ok"]:
        raise conflict("submission_no_longer_valid", "the submission no longer passes its checks; reject it or ask for a new submission", checks=report)
    s.status = "approved"
    s.decided_at = datetime.now(UTC)
    t.status = "done"
    bump(t)
    return report


def reject(db: Session, s: TaskSubmission, t: Task) -> None:
    features.require_write(db, "tasks", task_id=t.id)
    s.status = "rejected"
    s.decided_at = datetime.now(UTC)
    if t.status == "in_review":
        t.status = "in_progress"
        bump(t)


def lock_for_decision(db: Session, s: TaskSubmission) -> tuple[TaskSubmission, Task]:
    t = lock_task(db, s.task_id)
    return _lock_submission(db, s.id), t
