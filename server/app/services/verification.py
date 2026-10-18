"""Verification state of derived artifacts and later standard checks (W7, AC-T04 / AC-T08).

Three separate dimensions (W0 §2.2): execution (the run), engineering check and acceptance. A model revision or a
drawing package carries the checks recorded when it was made (``checks`` JSON: a real report in standard runs, a
``skipped_turbo`` record in Turbo runs). A person can later run a standard check on the exact artifact
(``POST /revisions/{id}/checks``, ``POST /drawings/{id}/checks``): the result is a new append-only
``artifact_checks`` row bound to the artifact's bytes; the creation record is never rewritten. ``verification``
reads the newest record per check for the current bytes.

An artifact stays unverified until BOTH hold (W0 §2.2):

(a) every business check skipped at creation was re-run in standard and passed. ``model.grounding`` /
    ``model.gate`` cannot be re-run honestly against values no person stated: they count as satisfied once (b)
    holds and the standard conformance / feature re-checks passed;
(b) every ``ai_turbo`` / ``rule`` assumption it carries was confirmed by a person
    (``POST /revisions/{id}/assumptions/{aid}/confirm``): an append-only ``assumption.confirmation`` record with
    author person, who, when and the confirmed value. A different value is a correction: it is recorded, the
    artifact stays unverified, and the change goes through a new revision (never an in-place edit).

The block lists ``pending_checks`` and ``unconfirmed_assumptions``; a drawing also reports
``upstream_unverified`` while its source revision is unverified.

S5 (docs/agent/stability-fixes.zh-CN.md §S5) adds the same decisions for assembly revisions
(``POST /assembly-revisions/{id}/assumptions/{aid}/confirm``) and engineering spec versions, and one place that lists
every AI / rule assumption upstream of a product and lets a person confirm them in one go
(:func:`upstream_assumptions`, :func:`confirm_upstream`). A standard step that takes over a Turbo spec needs every
assumption of that spec confirmed first (:func:`require_confirmed_spec`); the confirmer is always the person.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from .. import policy as exec_policy
from ..errors import AppError, not_found
from ..models import (
    ArtifactCheck,
    Assembly,
    AssemblyComponent,
    AssemblyRevision,
    Drawing,
    EngineeringSpec,
    EngineeringSpecVersion,
    ModelRevision,
    Part,
    User,
)
from ..storage import read_blob
from .audit import audit

REVISION_CHECKS = ("model.conformance", "geometry.features")
DRAWING_CHECKS = ("drawing.annotation", "drawing.dwg_semantic")
#: The record a person's assumption decision leaves (status passed = confirmed, failed = corrected).
CONFIRMATION = "assumption.confirmation"
#: Checks a Turbo revision skipped that no later run can repeat (they judge values no person stated): satisfied by
#: the person confirming every assumption plus passed standard conformance / feature re-checks.
PERSON_SATISFIED = ("model.grounding", "model.gate")
#: Statuses that keep a check pending (not_requested is no requirement, passed is done).
_OPEN = ("skipped_turbo", "pending", "failed", "unknown")


def _iso(dt: Any) -> str | None:
    return dt.isoformat() if dt else None


# --------------------------------------------------------------------------- #
# assumptions
# --------------------------------------------------------------------------- #


def revision_assumptions(rev: ModelRevision | None) -> list[dict[str, Any]]:
    """The assumption records a Turbo revision was built on (``checks.assumptions``)."""
    checks = rev.checks if rev is not None and isinstance(rev.checks, dict) else {}
    return [a for a in checks.get("assumptions") or [] if isinstance(a, dict)]


def assumption_refs(rev: ModelRevision | None) -> list[dict[str, Any]]:
    """The revision's assumption records, each with its owner (a derived artifact carries references to the decisions
    it rests on; W0 §2.3)."""
    if rev is None:
        return []
    owner: dict[str, Any] = {"type": "model_revision", "id": str(rev.id), "rev_no": rev.rev_no}
    db = object_session(rev)
    part = db.get(Part, rev.part_id) if db is not None else None
    if part is not None:  # lists of decisions over several parts say which part each one belongs to
        owner["label"] = f"{part.number} R{rev.rev_no}"
    return [{**a, "owner": owner} for a in revision_assumptions(rev)]


# --------------------------------------------------------------------------- #
# records
# --------------------------------------------------------------------------- #


def _subject_id(row: ArtifactCheck) -> uuid.UUID | None:
    return row.model_revision_id or row.drawing_id or row.assembly_revision_id or row.spec_version_id


def check_out(row: ArtifactCheck) -> dict[str, Any]:
    return {"id": str(row.id), "subject_type": row.subject_type,
            "subject_id": str(_subject_id(row)), "subject_sha": row.subject_sha, "check": row.check,
            "execution_policy": row.execution_policy, "status": row.status, "report": row.report or {},
            "requested_by": str(row.requested_by) if row.requested_by else None, "created_at": _iso(row.created_at)}


def _rows(db: Session, *, revision_id: uuid.UUID | None = None, drawing_id: uuid.UUID | None = None,
          assembly_revision_id: uuid.UUID | None = None, spec_version_id: uuid.UUID | None = None) -> list[ArtifactCheck]:
    stmt = select(ArtifactCheck).order_by(ArtifactCheck.created_at, ArtifactCheck.id)
    if revision_id:
        stmt = stmt.where(ArtifactCheck.model_revision_id == revision_id)
    elif assembly_revision_id:
        stmt = stmt.where(ArtifactCheck.assembly_revision_id == assembly_revision_id)
    elif spec_version_id:
        stmt = stmt.where(ArtifactCheck.spec_version_id == spec_version_id)
    else:
        stmt = stmt.where(ArtifactCheck.drawing_id == drawing_id)
    return list(db.scalars(stmt).all())


def _latest(rows: list[ArtifactCheck], sha: str | None) -> dict[str, ArtifactCheck]:
    """Newest record per check that is bound to the artifact's current bytes."""
    out: dict[str, ArtifactCheck] = {}
    for r in rows:
        if r.subject_sha == sha:
            out[r.check] = r
    return out


def list_checks(db: Session, *, revision: ModelRevision | None = None, drawing: Drawing | None = None,
                assembly_revision: AssemblyRevision | None = None, spec_version: EngineeringSpecVersion | None = None,
                confirmations: bool = False) -> list[dict[str, Any]]:
    """Later check records (default) or the assumption confirmation records of one artifact, oldest first."""
    if revision is not None:
        rows = _rows(db, revision_id=revision.id)
    elif assembly_revision is not None:
        rows = _rows(db, assembly_revision_id=assembly_revision.id)
    elif spec_version is not None:
        rows = _rows(db, spec_version_id=spec_version.id)
    else:
        rows = _rows(db, drawing_id=drawing.id if drawing else None)
    return [check_out(r) for r in rows if (r.check == CONFIRMATION) is confirmations]


def _decisions(rows: list[ArtifactCheck], sha: str | None) -> dict[str, ArtifactCheck]:
    """Newest person decision per assumption id, bound to the artifact's current bytes."""
    out: dict[str, ArtifactCheck] = {}
    for r in rows:
        if r.check == CONFIRMATION and r.subject_sha == sha:
            aid = str((r.report or {}).get("assumption_id") or "")
            if aid:
                out[aid] = r
    return out


def _assumption_state(assumptions: list[dict[str, Any]], decisions: dict[str, ArtifactCheck],
                      owner: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """(unconfirmed, corrected, confirmed) refs of the AI / rule assumptions an artifact carries."""
    unconfirmed: list[dict[str, Any]] = []
    corrected: list[dict[str, Any]] = []
    confirmed: list[dict[str, Any]] = []
    for a in assumptions:
        if not exec_policy.needs_confirmation(a):
            continue
        ref = exec_policy.assumption_ref(a, owner)
        row = decisions.get(str(a.get("id")))
        if row is None:
            unconfirmed.append(ref)
            continue
        rep = row.report or {}
        rec = {**ref, "decision": rep.get("decision"), "value": rep.get("value"), "by": rep.get("by"), "at": rep.get("at"),
               "record_id": str(row.id)}
        (confirmed if row.status == "passed" else corrected).append(rec)
    return unconfirmed, corrected, confirmed


# --------------------------------------------------------------------------- #
# verification blocks
# --------------------------------------------------------------------------- #


def _report_status(report: Any) -> str:
    """A creation-time check record → CHECK_STATUSES. A report without a verdict is unknown, never passed."""
    if not isinstance(report, dict):
        return "not_requested"
    st = report.get("status")
    if st in ("passed", "failed", "skipped_turbo"):
        return str(st)
    if report.get("error"):
        return "unknown"
    return "not_requested"  # not_evaluated: nothing geometric to check


def _combine(statuses: list[str]) -> str:
    """Overall check status: failed > unknown > skipped_turbo > pending > not_requested; passed only when all passed."""
    if not statuses:
        return "not_requested"
    for st in ("failed", "unknown", "skipped_turbo", "pending", "not_requested"):
        if st in statuses:
            return st
    return "passed"


def _revision_owner(rev: ModelRevision) -> dict[str, Any]:
    return {"type": "model_revision", "id": str(rev.id), "rev_no": rev.rev_no}


def revision_verification(rev: ModelRevision, db: Session | None = None) -> dict[str, Any]:
    """``policy.verification`` of a model revision (+ ``checks``: status per check name; ``satisfied_checks``: the
    skipped checks a person's confirmation stands in for; ``corrected_assumptions`` / ``confirmed_assumptions``)."""
    db = db or object_session(rev)
    checks = rev.checks if isinstance(rev.checks, dict) else {}
    policy = checks.get("execution_policy") or "standard"
    per = {"model.conformance": _report_status(checks.get("conformance"))}
    for key in ("grounding", "gate"):
        if key in checks:
            per[f"model.{key}"] = _report_status(checks.get(key))
    features = checks.get("features")
    for c in (features.get("checks") or []) if isinstance(features, dict) else []:
        if isinstance(c, dict) and c.get("check"):
            per[str(c["check"])] = str(c.get("status") or "unknown")
    rows = _rows(db, revision_id=rev.id) if db is not None and rev.id is not None else []
    for name, row in _latest([r for r in rows if r.check != CONFIRMATION], rev.step_sha).items():
        per[name] = row.status
    assumptions = revision_assumptions(rev)
    unconfirmed, corrected, confirmed = _assumption_state(assumptions, _decisions(rows, rev.step_sha), _revision_owner(rev))
    effective = dict(per)
    satisfied: dict[str, Any] = {}
    rechecked = [k for k in per if k not in PERSON_SATISFIED]
    if (not unconfirmed and not corrected and any(per[k] == "passed" for k in rechecked)
            and all(per[k] in ("passed", "not_requested") for k in rechecked)):
        for k in PERSON_SATISFIED:
            if per.get(k) == "skipped_turbo":
                effective[k] = "passed"
                satisfied[k] = {"by": "person_confirmation", "assumptions": [c["id"] for c in confirmed],
                                "rechecked": sorted(r for r in rechecked if per[r] == "passed")}
    pending = sorted(k for k, st in effective.items() if st in _OPEN)
    out = exec_policy.verification(policy, _combine(list(effective.values())), "not_requested", assumptions,
                                   pending_checks=pending, unconfirmed_assumptions=unconfirmed)
    if corrected:
        out["unverified"] = True
    out.update(checks=per, satisfied_checks=satisfied, corrected_assumptions=corrected, confirmed_assumptions=confirmed)
    return out


def blocking(v: dict[str, Any] | None) -> bool:
    """Whether a verification block is unverified for a reason a person can act on: pending checks, unconfirmed /
    corrected assumptions or an unverified upstream. A manual revision without a requirement list simply has no
    check (``not_requested``): that is no skipped check and does not taint what is derived from it."""
    if not v:
        return False
    return bool(v.get("pending_checks") or v.get("unconfirmed_assumptions") or v.get("corrected_assumptions")
                or v.get("upstream_unverified"))


def _dwg_status(d: Drawing) -> str | None:
    """Creation status of the DWG semantic readback; None when the package holds no DWG (nothing to verify)."""
    st = ((d.checks or {}).get("dwg") or {}).get("status")
    return {"passed": "passed", "unverified": "skipped_turbo"}.get(str(st))


def drawing_verification(d: Drawing, db: Session | None = None) -> dict[str, Any]:
    """``policy.verification`` of a drawing package: its annotation check, the DWG readback when it holds DWG files,
    its own assumptions and the source revision. A package drawn from an unverified revision is unverified too
    (``upstream_unverified``) and lists that revision's unconfirmed assumptions with their owner."""
    db = db or object_session(d)
    checks = d.checks if isinstance(d.checks, dict) else {}
    policy = checks.get("execution_policy") or "standard"
    per: dict[str, str] = {"drawing.annotation": _report_status(checks.get("annotation"))}
    dwg = _dwg_status(d)
    if dwg is not None:
        per["drawing.dwg_semantic"] = dwg
    rev = None
    rows: list[ArtifactCheck] = []
    if db is not None:
        if d.id is not None:
            rows = _rows(db, drawing_id=d.id)
            for name, row in _latest([r for r in rows if r.check != CONFIRMATION], d.manifest_sha256).items():
                per[name] = row.status
        rev = db.get(ModelRevision, d.model_revision_id) if d.model_revision_id else None
    stored = [a for a in checks.get("assumptions") or [] if isinstance(a, dict)]
    assumptions = stored or assumption_refs(rev)
    # The drawing's own decisions (references to the revision's assumptions are decided on the revision).
    own = [a for a in stored if not a.get("owner")]
    unconfirmed, corrected, confirmed = _assumption_state(own, _decisions(rows, d.manifest_sha256),
                                                          {"type": "drawing", "id": str(d.id)} if d.id is not None else None)
    up = revision_verification(rev, db) if rev is not None else None
    upstream = None
    if up is not None and rev is not None and blocking(up):
        upstream = {**_revision_owner(rev), "pending_checks": up["pending_checks"],
                    "unconfirmed_assumptions": [a.get("id") for a in up["unconfirmed_assumptions"]],
                    "corrected_assumptions": [a.get("id") for a in up.get("corrected_assumptions") or []]}
        unconfirmed = unconfirmed + list(up["unconfirmed_assumptions"])
    pending = sorted(k for k, st in per.items() if st in _OPEN)
    out = exec_policy.verification(policy, _combine(list(per.values())), "not_requested", assumptions,
                                   pending_checks=pending, unconfirmed_assumptions=unconfirmed, upstream_unverified=upstream)
    if corrected:
        out["unverified"] = True
    out.update(checks=per, corrected_assumptions=corrected, confirmed_assumptions=confirmed)
    return out


# --------------------------------------------------------------------------- #
# later standard checks (AC-T08)
# --------------------------------------------------------------------------- #


def _append(db: Session, user: User, *, project_id: uuid.UUID, check: str, status: str, report: dict[str, Any],
            revision: ModelRevision | None = None, drawing: Drawing | None = None,
            assembly_revision: AssemblyRevision | None = None, spec_version: EngineeringSpecVersion | None = None) -> ArtifactCheck:
    if revision is not None:
        subject, sha = "model_revision", revision.step_sha
    elif assembly_revision is not None:
        subject, sha = "assembly_revision", assembly_revision.content_sha256
    elif spec_version is not None:
        subject, sha = "engineering_spec_version", spec_sha(spec_version)
    else:
        subject, sha = "drawing", drawing.manifest_sha256  # type: ignore[union-attr]
    row = ArtifactCheck(project_id=project_id, subject_type=subject,
                        model_revision_id=revision.id if revision is not None else None,
                        drawing_id=drawing.id if drawing is not None else None,
                        assembly_revision_id=assembly_revision.id if assembly_revision is not None else None,
                        spec_version_id=spec_version.id if spec_version is not None else None,
                        subject_sha=sha, check=check, execution_policy="standard", status=status, report=report, requested_by=user.id)
    db.add(row)
    db.flush()
    audit(db, action="artifact.assumption_decision" if check == CONFIRMATION else "artifact.check", subject_type=row.subject_type,
          subject_id=_subject_id(row),
          user_id=user.id, project_id=project_id, data={"check": check, "status": status, "check_id": str(row.id),
                                                        "subject_sha": row.subject_sha})
    return row


def _verified_blob(db: Session, sha: str | None, what: str) -> bytes:
    import hashlib

    if not sha:
        raise AppError(422, "nothing_to_check", f"the {what} has no stored file to check")
    try:
        data, _ = read_blob(db, sha)
    except FileNotFoundError:
        raise AppError(409, "artifact_file_missing", f"the {what} is missing from storage", {"sha256": sha})
    if hashlib.sha256(data).hexdigest() != sha:
        raise AppError(409, "artifact_file_mismatch", f"the {what} does not match its hash", {"sha256": sha})
    return data


def recheck_revision(db: Session, user: User, rev: ModelRevision, check: str = "model.conformance") -> list[ArtifactCheck]:
    """Run a standard check on the exact revision and append the result: ``model.conformance`` on its stored STEP
    bytes, or ``geometry.features`` (one record per ``geometry.feature.<kind>``) on a rebuild of its ops that must
    reproduce the revision's build key."""
    from ..ai.modeling_agent import checker_error
    from . import revisions as rev_svc

    if check not in REVISION_CHECKS:
        raise AppError(422, "unknown_check", f"a model revision can be checked with: {', '.join(REVISION_CHECKS)}", {"check": check})
    if rev.status != "ready" or rev.mesh_only:
        raise AppError(422, "nothing_to_check", "only a ready revision with exact geometry can be checked")
    if check == "geometry.features":
        return _recheck_features(db, user, rev)
    if rev.requirements is None:
        raise AppError(422, "no_requirements", "this revision has no requirement list to check against")
    part = db.get(Part, rev.part_id)
    assert part is not None
    step = _verified_blob(db, rev.step_sha, "revision STEP")
    mode = (rev.checks or {}).get("mode") or "create"
    try:
        report = exec_policy.run_check(check, "standard", lambda: rev_svc.check_step(step, list(rev.requirements or []), mode=mode))
    except Exception as exc:  # noqa: BLE001 - a checker crash is an unknown result, never a pass
        report = {"status": "not_evaluated", "error": f"{type(exc).__name__}: {str(exc)[:300]}", "items": [], "unrequested": []}
    if report.get("status") in ("passed", "failed") and checker_error(report) is None:
        status = str(report["status"])
    elif checker_error(report) is not None:
        status = "unknown"
    else:
        status = "not_requested"
    return [_append(db, user, project_id=part.project_id, check=check, status=status, report=report, revision=rev)]


def _recheck_features(db: Session, user: User, rev: ModelRevision) -> list[ArtifactCheck]:
    """``geometry.feature.<kind>`` in standard on a rebuild of the revision's ops (W3 checks need the build traces).
    A rebuild that does not reproduce the revision's build key is an unknown result for every kind, never a pass."""
    from ..cad.feature_checks import feature_kinds
    from . import revisions as rev_svc

    part = db.get(Part, rev.part_id)
    assert part is not None
    spec = rev_svc.spec_of(rev)
    kinds = feature_kinds(spec)
    if not kinds:
        raise AppError(422, "nothing_to_check", "this revision has no geometry-library feature to check")
    _verified_blob(db, rev.step_sha, "revision STEP")
    result = rev_svc.build_spec(db, spec, export=False)
    if not result.ok or result.build_key != rev.build_key:
        reason = ("the ops no longer build" if not result.ok
                  else f"the rebuild has build key {result.build_key}, the revision {rev.build_key}")
        return [_append(db, user, project_id=part.project_id, check=f"geometry.feature.{k}", status="unknown",
                        report={"check": f"geometry.feature.{k}", "status": "unknown", "error": reason, "items": []}, revision=rev)
                for k in kinds]
    try:
        report = rev_svc.feature_check(spec, result, execution_policy="standard") or {"checks": []}
    except Exception as exc:  # noqa: BLE001 - a checker crash is an unknown result, never a pass
        report = {"checks": [{"check": f"geometry.feature.{k}", "status": "unknown", "items": [],
                              "error": f"{type(exc).__name__}: {str(exc)[:300]}"} for k in kinds]}
    rows = []
    for c in report.get("checks") or []:
        status = c.get("status") if c.get("status") in ("passed", "failed") else "unknown"
        rows.append(_append(db, user, project_id=part.project_id, check=str(c["check"]), status=status,
                            report={**c, "build_key": result.build_key}, revision=rev))
    return rows


# --------------------------------------------------------------------------- #
# person confirmation of assumptions (W0 §2.2 (b))
# --------------------------------------------------------------------------- #

_UNSET: Any = object()


def _same(a: Any, b: Any) -> bool:
    """Equal values, numbers compared as numbers (10 == 10.0), lists element by element."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(a)), abs(float(b)))
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    return a == b


def confirm_assumption(db: Session, user: User, *, revision: ModelRevision | None = None, drawing: Drawing | None = None,
                       assembly_revision: AssemblyRevision | None = None, spec_version: EngineeringSpecVersion | None = None,
                       assumption_id: str, value: Any = _UNSET, note: str = "") -> ArtifactCheck:
    """A person's decision on one AI / rule assumption of this exact artifact, appended as an
    ``assumption.confirmation`` record (author person, who, when, value). Without ``value`` (or with the adopted
    value) it is a confirmation (status passed). A different value is a correction (status failed): recorded, but
    the artifact stays unverified, since a corrected value is an engineering change made through a new revision.
    An assumption the AI left open (adopted null) can only be confirmed as built; stating a value corrects it.

    An assembly revision decides only its own assumptions; the ones it copied from its spec version are decided on
    that spec version (:func:`assembly_assumption_owner` routes them)."""
    from datetime import UTC, datetime

    if sum(x is not None for x in (revision, drawing, assembly_revision, spec_version)) != 1:
        raise ValueError("pass exactly one of revision, drawing, assembly_revision, spec_version")
    project_id: uuid.UUID | None = None
    part_id: uuid.UUID | None = None
    if revision is not None:
        items = revision_assumptions(revision)
        part_id = revision.part_id
    elif assembly_revision is not None:
        items = assembly_own_assumptions(db, assembly_revision)
        asm = db.get(Assembly, assembly_revision.assembly_id)
        assert asm is not None
        project_id = asm.project_id
    elif spec_version is not None:
        if spec_version.status not in ("ready", "needs_answers"):
            raise AppError(409, "spec_not_ready", f"engineering spec version {spec_version.version_no} is {spec_version.status}",
                           {"version_id": str(spec_version.id), "status": spec_version.status})
        items = spec_assumptions(spec_version)
        project_id = spec_version.project_id
    else:
        items = [a for a in ((drawing.checks or {}) if isinstance(drawing.checks, dict) else {}).get("assumptions") or []  # type: ignore[union-attr]
                 if isinstance(a, dict) and not a.get("owner")]
        part_id = drawing.part_id  # type: ignore[union-attr]
    a = next((x for x in items if str(x.get("id")) == assumption_id), None)
    if a is None:
        raise AppError(404, "assumption_not_found", f"this artifact carries no assumption {assumption_id!r}",
                       {"assumption_id": assumption_id, "assumptions": [str(x.get("id")) for x in items]})
    if (a.get("author") or "ai_turbo") not in exec_policy.NEEDS_CONFIRMATION:
        raise AppError(422, "not_an_ai_assumption", "this value was stated by a person; there is nothing to confirm",
                       {"assumption_id": assumption_id, "author": a.get("author")})
    adopted = a.get("adopted")
    if value is _UNSET or (adopted is not None and _same(value, adopted)):
        decision, final = "confirmed", adopted
    else:
        decision, final = "corrected", value
    if project_id is None:
        part = db.get(Part, part_id)
        assert part is not None
        project_id = part.project_id
    report = {"assumption_id": assumption_id, "subject": a.get("subject"), "field": a.get("field"), "unit": a.get("unit"),
              "assumption_author": a.get("author") or "ai_turbo", "adopted": adopted, "value": final, "decision": decision,
              "author": "person", "by": {"id": str(user.id), "name": user.name}, "at": datetime.now(UTC).isoformat(),
              "note": (note or "")[:1000]}
    if decision == "corrected":
        report["engineering_change"] = (
            "a corrected value changes the engineering content: answer it on the spec page (a new spec version)"
            if spec_version is not None else
            "a corrected value changes the engineering content: make a new revision with it (edit or AI modify); "
            "this revision stays unverified")
    return _append(db, user, project_id=project_id, check=CONFIRMATION, status="passed" if decision == "confirmed" else "failed",
                   report=report, revision=revision, drawing=drawing, assembly_revision=assembly_revision, spec_version=spec_version)


def recheck_drawing(db: Session, user: User, d: Drawing, check: str = "drawing.annotation") -> ArtifactCheck:
    """Run a standard drawing check on the package's exact files and append the result: the annotation check against
    the source revision's STEP and requirements, or the DWG semantic readback of every stored DWG page."""
    if check not in DRAWING_CHECKS:
        raise AppError(422, "unknown_check", f"a drawing can be checked with: {', '.join(DRAWING_CHECKS)}", {"check": check})
    if d.manifest is None or not d.pages:
        raise AppError(422, "nothing_to_check", "this drawing has no package to check")
    part = db.get(Part, d.part_id)
    assert part is not None
    pages: list[dict[str, Any]] = []
    for p in d.pages:
        files = {f.get("format"): f for f in p.get("files") or []}
        pages.append({**p, "svg": _verified_blob(db, (files.get("svg") or {}).get("sha256"), f"page {p.get('index')} SVG"),
                      "dxf": _verified_blob(db, (files.get("dxf") or {}).get("sha256"), f"page {p.get('index')} DXF"),
                      "dwg_sha": (files.get("dwg") or {}).get("sha256")})
    if check == "drawing.annotation":
        from ..cad.annotation_check import check_package
        from .revisions import KERNEL_LOCK

        rev = db.get(ModelRevision, d.model_revision_id) if d.model_revision_id else None
        step = _verified_blob(db, rev.step_sha, "revision STEP") if rev is not None and rev.step_sha else None
        reqs = list(rev.requirements) if rev is not None and rev.requirements is not None else None
        with KERNEL_LOCK:
            report = exec_policy.run_check(check, "standard", lambda: check_package(
                pages, step_bytes=step, requirements=reqs, features=rev.features if rev is not None else None)).summary()
        status = "passed" if report.get("status") == "passed" else "failed"
        return _append(db, user, project_id=part.project_id, check=check, status=status, report=report, drawing=d)
    # drawing.dwg_semantic
    from ..cad.dwg import DwgError, readback_dwg
    from ..config import get_settings
    from .drawings import _dwg_runtime

    dwg_pages = [p for p in pages if p["dwg_sha"]]
    if not dwg_pages:
        raise AppError(422, "nothing_to_check", "this drawing package holds no DWG file")
    results: list[dict[str, Any]] = []
    status = "passed"

    def run() -> dict[str, Any]:
        nonlocal status
        runtime = _dwg_runtime()
        for p in dwg_pages:
            data = _verified_blob(db, p["dwg_sha"], f"page {p.get('index')} DWG")
            try:
                checks = readback_dwg(p["dxf"], data, runtime=runtime, timeout_s=get_settings().dwg_timeout_s)
            except DwgError as exc:
                results.append({"index": p.get("index"), "error": {"code": exc.code, "message": exc.message}})
                status = "unknown"
                continue
            states = [c.status for c in checks]
            results.append({"index": p.get("index"), "readback": [c.model_dump(mode="json") for c in checks]})
            if "failed" in states:
                status = "failed"
            elif any(s != "passed" for s in states) and status == "passed":
                status = "unknown"  # an independent reader that did not run is no pass
        return {"status": status, "pages": results}

    report = exec_policy.run_check(check, "standard", run)
    return _append(db, user, project_id=part.project_id, check=check, status=status, report=report, drawing=d)


# --------------------------------------------------------------------------- #
# S5: spec versions, assembly revisions and every assumption upstream of a product
# --------------------------------------------------------------------------- #

SPEC_GATE = "spec.clarification_gate"
#: Error codes of a standard step that takes over a Turbo spec version (S5; replaces the old 409 spec_policy_mismatch).
UNCONFIRMED = "upstream_assumptions_unconfirmed"
CORRECTED = "upstream_assumptions_corrected"
#: Product kinds the upstream listing starts from.
SOURCE_TYPES = ("engineering_spec_version", "assembly_revision", "model_revision")
_MAX_DEPTH = 12  # nested assemblies (W0 §7: ≤ 10 levels)
#: Assembly checks that run on every leaf of the whole tree (a child sub-assembly is covered by its parent's run).
TREE_CHECKS = ("assembly.interference", "assembly.clearance")


def spec_sha(v: EngineeringSpecVersion) -> str:
    """Canonical hash of a finished spec version's content (immutable once extracted): what a decision is bound to."""
    return hashlib.sha256(json.dumps(v.content or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def spec_assumptions(v: EngineeringSpecVersion | None) -> list[dict[str, Any]]:
    """The values a Turbo extraction adopted (``content.assumptions``; author ``ai_turbo``)."""
    return [a for a in ((v.content or {}).get("assumptions") or []) if isinstance(a, dict)] if v is not None else []


def spec_owner(v: EngineeringSpecVersion) -> dict[str, Any]:
    return {"type": "engineering_spec_version", "id": str(v.id), "version_no": v.version_no}


def assembly_owner(rev: AssemblyRevision) -> dict[str, Any]:
    return {"type": "assembly_revision", "id": str(rev.id), "rev_no": rev.rev_no}


def _spec_state(db: Session, v: EngineeringSpecVersion) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    rows = _rows(db, spec_version_id=v.id) if v.id is not None else []
    return _assumption_state(spec_assumptions(v), _decisions(rows, spec_sha(v)), spec_owner(v))


def spec_decisions(db: Session, v: EngineeringSpecVersion) -> dict[str, dict[str, Any]]:
    """Person decisions on the spec's assumptions by id ({decision, value, by, at}); the newest one counts."""
    _, corrected, confirmed = _spec_state(db, v)
    return {str(x["id"]): x for x in corrected + confirmed}


def spec_verification(db: Session, v: EngineeringSpecVersion) -> dict[str, Any]:
    """``policy.verification`` of a spec version with the person decisions on its assumptions. The clarification gate a
    Turbo extraction skipped cannot be re-run against values no person stated: it counts as satisfied once a person
    confirmed every assumption (W0 §2.2, like ``model.grounding`` / ``model.gate``) — and only then: a Turbo version
    with no adopted value has nothing a person confirmed, so its skipped gate stays open (never passed by default)."""
    gate = next((c for c in v.checks or [] if isinstance(c, dict) and c.get("check") == SPEC_GATE), None)
    status = str((gate or {}).get("status") or ("pending" if v.status == "extracting" else "not_requested"))
    if status not in ("not_requested", "skipped_turbo", "pending", "passed", "failed", "unknown"):
        status = "unknown"
    unconfirmed, corrected, confirmed = _spec_state(db, v)
    per = {SPEC_GATE: status}
    effective = dict(per)
    satisfied: dict[str, Any] = {}
    if (status == "skipped_turbo" and confirmed and not unconfirmed and not corrected
            and v.status in ("ready", "needs_answers")):
        effective[SPEC_GATE] = "passed"
        satisfied[SPEC_GATE] = {"by": "person_confirmation", "assumptions": [c["id"] for c in confirmed], "rechecked": []}
    pending = sorted(k for k, st in effective.items() if st in _OPEN)
    out = exec_policy.verification(v.execution_policy, _combine(list(effective.values())), "not_requested",  # type: ignore[arg-type]
                                   spec_assumptions(v), pending_checks=pending, unconfirmed_assumptions=unconfirmed)
    if corrected:
        out["unverified"] = True
    out.update(checks=per, satisfied_checks=satisfied, corrected_assumptions=corrected, confirmed_assumptions=confirmed)
    return out


def require_confirmed_spec(db: Session, v: EngineeringSpecVersion) -> None:
    """A standard step may take over a Turbo spec version only once a person confirmed every value it adopted (S5).

    409 ``upstream_assumptions_unconfirmed`` lists ALL of them with the entry that confirms them in one go (never the
    old dead end asking to re-extract); 409 ``upstream_assumptions_corrected`` when a person corrected one: that is an
    engineering change, made as a new spec version. A standard spec passes unchanged."""
    if not exec_policy.is_turbo(v.execution_policy):
        return
    unconfirmed, corrected, _ = _spec_state(db, v)
    source = {"type": "engineering_spec_version", "id": str(v.id)}
    entry = {"method": "POST", "path": f"/projects/{v.project_id}/upstream-assumptions/confirm", "source": source,
             "list": f"/projects/{v.project_id}/upstream-assumptions?source_type=engineering_spec_version&source_id={v.id}"}
    if corrected:
        raise AppError(409, CORRECTED, f"a person corrected {len(corrected)} value(s) of this Turbo spec version; answer them on the "
                       "spec page (a new spec version) and run with that version", {
                           "spec_version_id": str(v.id), "spec_policy": "turbo", "corrected": corrected, "recovery": "new_spec_version"})
    if unconfirmed:
        raise AppError(409, UNCONFIRMED, f"this spec version was extracted in Turbo: a person must confirm its {len(unconfirmed)} "
                       "AI-adopted value(s) before a standard run uses them", {
                           "spec_version_id": str(v.id), "spec_policy": "turbo", "count": len(unconfirmed), "pending": unconfirmed,
                           "confirm": entry, "recovery": "confirm_assumptions"})


def assembly_spec_version(db: Session, rev: AssemblyRevision) -> EngineeringSpecVersion | None:
    return db.get(EngineeringSpecVersion, rev.spec_version_id) if rev.spec_version_id else None


def _is_copy(a: dict[str, Any], spec_pairs: set[tuple[str, str]]) -> bool:
    return (str(a.get("id")), str(a.get("subject"))) in spec_pairs


def _split(db: Session, rev: AssemblyRevision) -> tuple[EngineeringSpecVersion | None, list[dict[str, Any]], set[str]]:
    """(spec version, the revision's own assumptions, ids of the copies of its spec's assumptions)."""
    spec_v = assembly_spec_version(db, rev)
    pairs = {(str(a.get("id")), str(a.get("subject"))) for a in spec_assumptions(spec_v)}
    items = [a for a in rev.assumptions or [] if isinstance(a, dict)]
    return spec_v, [a for a in items if not _is_copy(a, pairs)], {str(a.get("id")) for a in items if _is_copy(a, pairs)}


def assembly_own_assumptions(db: Session, rev: AssemblyRevision) -> list[dict[str, Any]]:
    """The assumptions this assembly revision decided itself (relation datums, rule mappings), not its spec's copies."""
    return _split(db, rev)[1]


def assembly_assumptions_with_owner(db: Session, rev: AssemblyRevision) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Every assumption the revision carries with the version that decides it: its spec version for a copy of the
    spec's (matched by id and subject), else the revision itself. Ids repeat across owners in older revisions."""
    spec_v = assembly_spec_version(db, rev)
    pairs = {(str(a.get("id")), str(a.get("subject"))) for a in spec_assumptions(spec_v)}
    own = assembly_owner(rev)
    return [(a, spec_owner(spec_v) if spec_v is not None and _is_copy(a, pairs) else own)
            for a in rev.assumptions or [] if isinstance(a, dict)]


def assembly_assumption_owner(db: Session, rev: AssemblyRevision, assumption_id: str) -> EngineeringSpecVersion | None:
    """The spec version that decides ``assumption_id`` of this revision (a copy of the spec's), else None (its own).
    A bare id that names both (older revisions) is the revision's own; the spec's copy is decided by its owner-qualified
    key (:func:`confirm_upstream`)."""
    spec_v, own, copies = _split(db, rev)
    if assumption_id in copies and not any(str(a.get("id")) == assumption_id for a in own):
        return spec_v
    return None


def _tree(db: Session, rev: AssemblyRevision) -> tuple[list[AssemblyRevision], list[ModelRevision]]:
    """Child assembly revisions and unique part revisions this exact revision places (suppressed instances left out)."""
    asms: dict[uuid.UUID, AssemblyRevision] = {}
    parts: dict[uuid.UUID, ModelRevision] = {}
    stack = [(rev.id, 0)]
    while stack:
        rid, depth = stack.pop()
        if depth > _MAX_DEPTH:
            continue
        rows = db.scalars(select(AssemblyComponent).where(AssemblyComponent.revision_id == rid, AssemblyComponent.suppressed.is_(False))
                          .order_by(AssemblyComponent.position)).all()
        for c in rows:
            if c.definition_kind == "part" and c.model_revision_id and c.model_revision_id not in parts:
                prev = db.get(ModelRevision, c.model_revision_id)
                if prev is not None:
                    parts[prev.id] = prev
            elif (c.definition_kind == "assembly" and c.child_revision_id and c.child_revision_id not in asms
                  and c.child_revision_id != rev.id):
                child = db.get(AssemblyRevision, c.child_revision_id)
                if child is not None:
                    asms[child.id] = child
                    stack.append((child.id, depth + 1))
    return list(asms.values()), list(parts.values())


def _assembly_state(db: Session, rev: AssemblyRevision) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """(unconfirmed, corrected, confirmed) of the assumptions the revision carries: its own decided here, its spec's
    copies decided on the spec version (listed with that owner)."""
    spec_v, own, copies = _split(db, rev)
    rows = _rows(db, assembly_revision_id=rev.id) if rev.id is not None else []
    unconfirmed, corrected, confirmed = _assumption_state(own, _decisions(rows, rev.content_sha256), assembly_owner(rev))
    if spec_v is not None and copies:
        su, sc, sf = _spec_state(db, spec_v)
        unconfirmed += [x for x in su if str(x["id"]) in copies]
        corrected += [x for x in sc if str(x["id"]) in copies]
        confirmed += [x for x in sf if str(x["id"]) in copies]
    return unconfirmed, corrected, confirmed


def _upstream_of(db: Session, rev: AssemblyRevision) -> dict[str, Any] | None:
    """The unverified versions an assembly revision is made of: part revisions (their checks / assumptions) and child
    assembly revisions (their own checks / assumptions). None when every one is verified (or has nothing to verify)."""
    children, parts = _tree(db, rev)
    items: list[dict[str, Any]] = []
    for prev in parts:
        v = revision_verification(prev, db)
        if blocking(v):
            part = db.get(Part, prev.part_id)
            items.append({**_revision_owner(prev), "part_number": part.number if part else None, "pending_checks": v["pending_checks"],
                          "unconfirmed_assumptions": [a.get("id") for a in v["unconfirmed_assumptions"]],
                          "corrected_assumptions": [a.get("id") for a in v.get("corrected_assumptions") or []]})
    for child in children:
        v = assembly_verification(db, child, upstream=False)
        # The parent's interference / clearance check runs on every leaf of the whole tree: a child's own geometry
        # check is not needed again; its mates, BOM, spec check and assumptions are its own.
        pending = [c for c in v["pending_checks"] if c not in TREE_CHECKS]
        unconfirmed = [a.get("id") for a in v["unconfirmed_assumptions"]]
        corrected = [a.get("id") for a in v.get("corrected_assumptions") or []]
        if pending or unconfirmed or corrected:
            items.append({**assembly_owner(child), "pending_checks": pending, "unconfirmed_assumptions": unconfirmed,
                          "corrected_assumptions": corrected})
    if not items:
        return None
    return {"type": "assembly_inputs", "count": len(items), "items": items}


def assembly_verification(db: Session, rev: AssemblyRevision, *, upstream: bool = True) -> dict[str, Any]:
    """``policy.verification`` of an assembly revision (S5): verified only when every required W4 check's latest record
    passed (a check skipped by Turbo counts once a later standard run passed: ``pending_checks`` lists the rest), a
    person confirmed every AI / rule assumption it carries (its own here, its spec's copies on the spec version) and
    every part / child assembly revision it is made of is verified (``upstream_unverified``)."""
    from .assemblies import REQUIRED_CHECKS

    checks = rev.checks if isinstance(rev.checks, dict) else {}
    required = list(REQUIRED_CHECKS) + [k for k in ("assembly.spec", "assembly.clearance", "assembly.placement_geometry") if k in checks]
    per = {name: str((((checks.get(name) or {}).get("latest")) or {}).get("status") or "pending") for name in required}
    pending = sorted(k for k, st in per.items() if st in _OPEN)
    unconfirmed, corrected, confirmed = _assembly_state(db, rev)
    up = _upstream_of(db, rev) if upstream and rev.id is not None else None
    refs = [{"ref": f"assembly_revision:{rev.id}#{a.get('id')}", **a} for a in rev.assumptions or [] if isinstance(a, dict)]
    out = exec_policy.verification(rev.execution_policy, rev.check_status, rev.acceptance_status, refs,  # type: ignore[arg-type]
                                   pending_checks=pending, unconfirmed_assumptions=unconfirmed, upstream_unverified=up)
    if corrected:
        out["unverified"] = True
    out.update(checks=per, corrected_assumptions=corrected, confirmed_assumptions=confirmed)
    return out


def refresh_assembly_verification(db: Session, rev: AssemblyRevision) -> dict[str, Any]:
    """Recompute and store the revision's verification block (read by BOM, roll-up, exports, drawings, deliveries)."""
    rev.verification = assembly_verification(db, rev)
    db.flush()
    return rev.verification


def refresh_spec_dependents(db: Session, spec_versions: list[EngineeringSpecVersion]) -> None:
    """A decision on a spec version's values changes every assembly revision of its project built from it: refresh
    their stored verification (new drawing packages and revision briefs read it)."""
    for v in spec_versions:
        revs = db.scalars(select(AssemblyRevision).join(Assembly, Assembly.id == AssemblyRevision.assembly_id)
                          .where(AssemblyRevision.spec_version_id == v.id, Assembly.project_id == v.project_id)).all()
        for rev in revs:
            refresh_assembly_verification(db, rev)


# --- one list of every assumption upstream of a product, confirmed in one go ----------------------------------------------


def load_source(db: Session, source_type: str, source_id: uuid.UUID | str, project_id: uuid.UUID) -> Any:
    """The exact product of this project (404 elsewhere)."""
    if source_type not in SOURCE_TYPES:
        raise AppError(422, "invalid_source_type", f"source_type must be one of {', '.join(SOURCE_TYPES)}", {"source_type": source_type})
    try:
        sid = uuid.UUID(str(source_id))
    except ValueError:
        raise not_found(source_type.replace("_", " "))
    if source_type == "engineering_spec_version":
        v = db.get(EngineeringSpecVersion, sid)
        if v is None or v.project_id != project_id:
            raise not_found("engineering spec version")
        return v
    if source_type == "assembly_revision":
        rev = db.get(AssemblyRevision, sid)
        asm = db.get(Assembly, rev.assembly_id) if rev is not None else None
        if rev is None or asm is None or asm.project_id != project_id:
            raise not_found("assembly revision")
        return rev
    prev = db.get(ModelRevision, sid)
    part = db.get(Part, prev.part_id) if prev is not None else None
    if prev is None or part is None or part.project_id != project_id:
        raise not_found("revision")
    return prev


def source_brief(db: Session, obj: Any) -> dict[str, Any]:
    """{type, id, label, execution_policy} of a product (the ``source_version`` of a follow-up request)."""
    if isinstance(obj, EngineeringSpecVersion):
        spec = db.get(EngineeringSpec, obj.spec_id)
        return {"type": "engineering_spec_version", "id": str(obj.id), "execution_policy": obj.execution_policy,
                "label": f"SP-{spec.number} v{obj.version_no}" if spec else f"v{obj.version_no}"}
    if isinstance(obj, AssemblyRevision):
        asm = db.get(Assembly, obj.assembly_id)
        return {"type": "assembly_revision", "id": str(obj.id), "execution_policy": obj.execution_policy,
                "label": f"{asm.number if asm else ''} A{obj.rev_no}".strip()}
    part = db.get(Part, obj.part_id)
    checks = obj.checks if isinstance(obj.checks, dict) else {}
    policy = checks.get("execution_policy") if checks.get("execution_policy") in exec_policy.POLICIES else exec_policy.STANDARD
    return {"type": "model_revision", "id": str(obj.id), "execution_policy": policy,
            "label": f"{part.number if part else ''} R{obj.rev_no}".strip()}


def _owners(db: Session, obj: Any) -> list[Any]:
    """The versions whose own assumptions a product rests on, upstream first: spec version → assembly revisions (root,
    then children) → part revisions."""
    if not isinstance(obj, AssemblyRevision):
        return [obj]
    children, parts = _tree(db, obj)
    spec_v = assembly_spec_version(db, obj)
    out: list[Any] = [spec_v] if spec_v is not None else []
    out += [obj, *children]
    return out + parts


def _owner_state(db: Session, owner: Any) -> tuple[dict[str, Any], list[dict[str, Any]], tuple[list, list, list]]:
    """(owner brief with label, raw assumptions decided on this owner, (unconfirmed, corrected, confirmed))."""
    label = source_brief(db, owner)["label"]
    if isinstance(owner, EngineeringSpecVersion):
        return {**spec_owner(owner), "label": label}, spec_assumptions(owner), _spec_state(db, owner)
    if isinstance(owner, AssemblyRevision):
        own = assembly_own_assumptions(db, owner)
        rows = _rows(db, assembly_revision_id=owner.id)
        return ({**assembly_owner(owner), "label": label}, own,
                _assumption_state(own, _decisions(rows, owner.content_sha256), assembly_owner(owner)))
    rows = _rows(db, revision_id=owner.id)
    items = revision_assumptions(owner)
    return {**_revision_owner(owner), "label": label}, items, _assumption_state(items, _decisions(rows, owner.step_sha), _revision_owner(owner))


def _key(owner: dict[str, Any], aid: Any) -> str:
    return f"{owner['type']}:{owner['id']}#{aid}"


def upstream_assumptions(db: Session, *, project_id: uuid.UUID, source_type: str, source_id: uuid.UUID | str) -> dict[str, Any]:
    """Every AI / rule assumption a product rests on, in one list (S5 "Confirm these assumptions"): its own and those of every version
    upstream of it (spec version → assembly revisions → part revisions), each with its owner, basis and state
    (``unconfirmed`` / ``confirmed`` / ``corrected``, with who decided and when). Person values are not listed."""
    obj = load_source(db, source_type, source_id, project_id)
    items: list[dict[str, Any]] = []
    for owner in _owners(db, obj):
        brief, raw, (unconfirmed, corrected, confirmed) = _owner_state(db, owner)
        by_id = {str(a.get("id")): a for a in raw}
        for state, refs in (("unconfirmed", unconfirmed), ("corrected", corrected), ("confirmed", confirmed)):
            for r in refs:
                a = by_id.get(str(r.get("id"))) or {}
                rec = exec_policy.decision_record(a) if a else {}  # generation supplement D-02: read as a decision
                items.append({**r, "owner": brief, "key": _key(brief, r.get("id")), "state": state, "basis": a.get("basis") or "",
                              "alternatives": list(a.get("alternatives") or []), "trigger": rec.get("trigger"),
                              "affected": rec.get("affected") or [], "sources": rec.get("sources") or [], "problems": rec.get("problems") or []})
    counts = {s: sum(1 for i in items if i["state"] == s) for s in ("unconfirmed", "corrected", "confirmed")}
    return {"source": source_brief(db, obj), "items": items, "counts": counts, "pending": counts["unconfirmed"],
            "blocked": counts["corrected"] > 0}


def confirm_upstream(db: Session, user: User, *, project_id: uuid.UUID, source_type: str, source_id: uuid.UUID | str,
                     keys: list[str] | None = None, note: str = "") -> dict[str, Any]:
    """Confirm, as this person, every unconfirmed assumption upstream of a product (or the listed ``keys``) in one
    transaction: one ``assumption.confirmation`` record per value (author person, who, when, the adopted value). A
    value the person wants changed is not confirmed here: it is corrected on its own artifact (an engineering change).
    The stored verification of the assembly revisions involved is refreshed (the source's tree and every assembly
    revision built from a spec version whose values were confirmed). Flushes; the caller commits."""
    listing = upstream_assumptions(db, project_id=project_id, source_type=source_type, source_id=source_id)
    known = {i["key"] for i in listing["items"]}
    if keys is not None:
        missing = [k for k in keys if k not in known]
        if missing:
            raise AppError(404, "assumption_not_found", "some assumptions are not upstream of this product", {"keys": missing[:20]})
    wanted = set(keys) if keys is not None else None
    todo = [i for i in listing["items"] if i["state"] == "unconfirmed" and (wanted is None or i["key"] in wanted)]
    done: list[str] = []
    specs: dict[uuid.UUID, EngineeringSpecVersion] = {}
    for item in todo:
        owner = item["owner"]
        oid = uuid.UUID(str(owner["id"]))
        kwargs: dict[str, Any] = {}
        if owner["type"] == "engineering_spec_version":
            kwargs["spec_version"] = specs.setdefault(oid, db.get(EngineeringSpecVersion, oid))
        elif owner["type"] == "assembly_revision":
            kwargs["assembly_revision"] = db.get(AssemblyRevision, oid)
        else:
            kwargs["revision"] = db.get(ModelRevision, oid)
        confirm_assumption(db, user, assumption_id=str(item["id"]), note=note, **kwargs)
        done.append(item["key"])
    obj = load_source(db, source_type, source_id, project_id)
    if isinstance(obj, AssemblyRevision):
        for rev in [*_tree(db, obj)[0], obj]:  # children first: the root's upstream reads them
            refresh_assembly_verification(db, rev)
    refresh_spec_dependents(db, list(specs.values()))  # every other assembly revision built from those spec versions
    audit(db, action="assumptions.confirm_upstream", subject_type=source_type, subject_id=uuid.UUID(str(source_id)), user_id=user.id,
          project_id=project_id, data={"confirmed": len(done), "keys": done[:200]})
    out = upstream_assumptions(db, project_id=project_id, source_type=source_type, source_id=source_id)
    out["confirmed_now"] = done
    return out


# --------------------------------------------------------------------------- #
# Turbo boundary report and object states of a delivery (generation reliability N, design §2.8)
# --------------------------------------------------------------------------- #


def checks_report(db: Session, *, policy: str | None, node_run_id: uuid.UUID | None = None) -> dict[str, Any]:
    """The delivery's check report: ``{policy, closed, invoked_closed, invoked, skipped, basis, …}``.

    ``closed`` lists the checks the policy closes (``policy.TURBO_CLOSED_CHECKS`` under Turbo, none under standard);
    ``invoked_closed`` counts how often one of them really ran. Every number comes from the checks ledger
    (``checks_ledger``): the ``checks_ledger`` step each node attempt of the delivering run keeps, every agent tool call
    of the run (``agent_executions.tool_calls[].checks_ledger``) and the unit running now (the delivery step itself).
    ``basis``: ``run`` (the run of ``node_run_id``) or ``delivery`` (a delivery made outside a run: only this call).
    ``nodes_unrecorded`` / ``tool_calls_unrecorded``: node runs or tool calls of the run that ran without a ledger record
    (made before the ledger existed, or by an executor that keeps none) — their checks are not in the numbers."""
    from collections import Counter

    from .. import checks_ledger
    from ..models import AgentExecution, NodeRun

    pol = exec_policy.parse_policy(policy)
    invoked: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    out: dict[str, Any] = {"policy": pol, "closed": exec_policy.closed_checks(pol)}
    now = checks_ledger.current()
    nr = db.get(NodeRun, node_run_id) if node_run_id is not None else None
    if nr is not None:
        nodes_recorded, tools_recorded, tools_unrecorded = 0, 0, 0
        nodes_unrecorded: list[str] = []
        for row in db.scalars(select(NodeRun).where(NodeRun.run_id == nr.run_id).order_by(NodeRun.node_id)).all():
            led = checks_ledger.from_steps(row.steps)
            if led is not None:
                invoked.update(led["invoked"])
                skipped.update(led["skipped"])
            running = row.id == nr.id and now is not None  # this attempt's ledger is the open scope (added below)
            if led is not None or running:
                nodes_recorded += 1
            elif row.started_at is not None:  # ran, but kept no ledger
                nodes_unrecorded.append(row.node_id)
        for ex in db.scalars(select(AgentExecution).where(AgentExecution.run_id == nr.run_id)).all():
            for call in ex.tool_calls or []:
                led = call.get("checks_ledger") if isinstance(call, dict) else None
                if isinstance(led, dict):
                    tools_recorded += 1
                    invoked.update({str(k): int(v) for k, v in (led.get("invoked") or {}).items()})
                    skipped.update({str(k): int(v) for k, v in (led.get("skipped") or {}).items()})
                elif isinstance(call, dict):
                    tools_unrecorded += 1
        out.update(basis="run", run_id=str(nr.run_id), nodes_recorded=nodes_recorded, nodes_unrecorded=nodes_unrecorded,
                   tool_calls_recorded=tools_recorded, tool_calls_unrecorded=tools_unrecorded)
    else:
        out["basis"] = "delivery"
    if now is not None:
        invoked.update(now.invoked)
        skipped.update(now.skipped)
    out["invoked_closed"] = sum(n for name, n in invoked.items() if exec_policy.is_closed(name, pol))
    out["invoked"] = dict(sorted(invoked.items()))
    out["skipped"] = dict(sorted(skipped.items()))
    return out


def _file_present(f: Any) -> bool:
    """A level object's STEP / DWG entry has a real file (``protocol_unmet``: a file in another protocol, package M)."""
    return isinstance(f, dict) and f.get("status") in ("generated", "protocol_unmet")


def _file_complete(f: Any) -> bool:
    """… and it is the required one: generated, without a code saying it falls short (e.g. its package is partial)."""
    return isinstance(f, dict) and f.get("status") == "generated" and not f.get("code")


def _short_of_target(entry: dict[str, Any]) -> bool:
    """Fields packages L / M add to a level object, read when present (a missing one is unknown, never a failure):
    ``build_record`` (not usable, requirements not created, a placeholder) and ``placed`` (a part of it not placed)."""
    rec = entry.get("build_record")
    if isinstance(rec, dict) and (rec.get("usable") is False or rec.get("not_created") or rec.get("representation") == "placeholder"):
        return True
    placed = entry.get("placed")
    return isinstance(placed, dict) and bool(placed.get("status") in ("partial", "none") or placed.get("unplaced") or placed.get("local_only"))


def _links(d: dict[str, Any], keys: set[str]) -> bool:
    """A decision belongs to an object: owned by one of its keys (revision / package id), consumed there (package L's
    ``consumed_by``: the used object's id or one of the spec keys of its ``ref``, a key or a list) or naming it in
    ``affected``."""
    owner = d.get("owner") if isinstance(d.get("owner"), dict) else {}
    if str(owner.get("id")) in keys:
        return True
    for c in d.get("consumed_by") or []:
        if not isinstance(c, dict):
            continue
        obj = c.get("object") if isinstance(c.get("object"), dict) else {}
        ref = c.get("ref")
        if (obj.get("id") and str(obj["id"]) in keys) or any(str(r) in keys for r in (ref if isinstance(ref, list) else [ref]) if r):
            return True
    return any(str(a) in keys for a in d.get("affected") or [])


def level_states(db: Session, levels: dict[str, Any], *, decisions: dict[str, Any] | None,
                 source_checks: list[dict[str, Any]] | None, coverage: dict[str, Any] | None,
                 root_package_id: str | None = None) -> None:
    """Add ``state`` (``policy.object_state``) to every object of a layered delivery's ``levels``, in place.

    Generated: its STEP or DWG exists. Complete: every required file is there as required (a DWG the delivery does not
    require is not counted), nothing in package L / M's fields says it falls short and, for the root assembly, its
    coverage is complete. Simulated: its revision came from a simulated provider. AI decisions: the delivery's decisions
    still waiting for a person that belong to it (owner, ``consumed_by``, ``affected``; the root assembly also rests on
    every spec decision). Check status: its revision's and its drawing package's — passed only when all passed."""
    items = [d for d in (decisions or {}).get("items") or [] if isinstance(d, dict) and d.get("state") == "adopted"]

    def of_spec(d: dict[str, Any]) -> bool:
        return (d.get("owner") or {}).get("type") == "engineering_spec_version"

    pkg_checks = {str(s.get("id")): str(s.get("check_status")) for s in source_checks or [] if s.get("type") == "drawing_package"}
    for level in ("assembly", "subassembly", "part"):
        for entry in levels.get(level) or []:
            if not isinstance(entry, dict):
                continue
            step, dwg = entry.get("step"), entry.get("dwg") if isinstance(entry.get("dwg"), dict) else {}
            needs_dwg = dwg.get("code") != "not_required"
            files = [step, dwg] if needs_dwg else [step]
            generated = any(_file_present(f) for f in files)
            complete = all(_file_complete(f) for f in files) and not _short_of_target(entry)
            rid = str(entry.get("revision_id") or "")
            keys = {str(k) for k in (rid, entry.get("key"), entry.get("spec_key"), entry.get("number"), dwg.get("package_id")) if k}
            simulated, statuses, pkg = False, [], None
            if rid and level == "part":
                prev = db.get(ModelRevision, uuid.UUID(rid))
                if prev is not None:
                    simulated = bool(isinstance(prev.ai_summary, dict) and prev.ai_summary.get("simulated"))
                    statuses.append(str(revision_verification(prev, db).get("check_status")))
                pkg = root_package_id  # a part's detail sheets are in the root package
            elif rid:
                arev = db.get(AssemblyRevision, uuid.UUID(rid))
                if arev is not None:
                    simulated = bool((arev.spec_ref or {}).get("simulated"))
                    statuses.append(str(arev.check_status))
                pkg = str(dwg.get("package_id") or "") or None
                if level == "assembly" and coverage is not None and coverage.get("status") not in (None, "complete"):
                    complete = False
            if needs_dwg and pkg and pkg in pkg_checks:
                statuses.append(pkg_checks[pkg])
            ai = sum(1 for d in items if _links(d, keys) or (level == "assembly" and of_spec(d)))
            check = "passed" if statuses and all(s == "passed" for s in statuses) else None
            entry["state"] = exec_policy.object_state(generated=generated, complete=complete, simulated=simulated,
                                                      ai_decisions=ai, check_status=check)
